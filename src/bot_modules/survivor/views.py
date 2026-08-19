"""Survivor member-facing views: the Join button and the pick panel.

The Join button is a DynamicItem — it survives restarts via the dynamic-items
registry, so the pinned season announcement keeps working across deploys.
The pick panel is the §2.4 secondary flow: an ephemeral AFC/NFC dual select
(Discord's 25-option cap vs up to 32 legal teams early season), so casuals
never touch slash-command syntax.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from bot_modules.core import branding
from bot_modules.core.db_utils import open_db, open_db_immediate
from bot_modules.services.survivor_service import get_season
from bot_modules.survivor import logic
from bot_modules.survivor.embeds import (
    build_panel_embed,
    build_pick_confirm_embed,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.survivor")


def kickoff_label(ts: float, offset_hours: float) -> str:
    """Guild-local short label for a select option — autocomplete and select
    labels can't render Discord timestamps, so this is the one place we
    format a clock by hand (§6.6: stored UTC, rendered local)."""
    local = datetime.fromtimestamp(
        ts, timezone(timedelta(hours=offset_hours))
    )
    # Hand-rolled 12-hour clock: %-I is glibc-only and the test suite also
    # runs on a Windows runner where it raises.
    hour12 = ((local.hour - 1) % 12) + 1
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%a')} {hour12}:{local.minute:02d} {ampm}"


def _game_option(game: logic.OpenGame, offset_hours: float) -> discord.SelectOption:
    vs = "vs" if game.is_home else "at"
    return discord.SelectOption(
        label=f"{game.team} ({vs} {game.opponent} · "
        f"{kickoff_label(game.kickoff_ts, offset_hours)})",
        value=game.team,
    )


class PickPanel(discord.ui.View):
    """Ephemeral AFC/NFC dual select. One instance per invocation — state
    (season, week) is baked in; the placement itself re-validates everything
    through place_pick, so a stale panel can refuse but never corrupt."""

    def __init__(
        self,
        bot: Bot,
        season: dict,
        user_id: int,
        week: int,
        games: list[logic.OpenGame],
        offset_hours: float,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.season = season
        self.user_id = user_id
        self.week = week
        for label, conference in (("AFC", logic.AFC_TEAMS), ("NFC", logic.NFC_TEAMS)):
            side = [g for g in games if g.team in conference][:25]
            if not side:
                continue
            select = discord.ui.Select(
                placeholder=f"Pick an {label} team to win…",
                options=[_game_option(g, offset_hours) for g in side],
            )
            select.callback = self._make_callback(select)
            self.add_item(select)

    def _make_callback(self, select: discord.ui.Select):
        async def _cb(interaction: discord.Interaction) -> None:
            await submit_pick(
                interaction, self.season, self.user_id, self.week,
                select.values[0], edit=True,
            )

        return _cb


async def _accent(
    interaction: discord.Interaction, db_path, guild_id: int
) -> discord.Color | None:
    """Accent color resolved DM-safely. The pick button rides the last-call
    DM (2026-08-18), where ``interaction.guild`` is None — resolve the
    season's guild instead, and fall back to the branding default (None →
    the builders' own fallback) if the guild is gone."""
    guild = interaction.guild or interaction.client.get_guild(guild_id)
    if guild is None:
        return None
    return await branding.resolve_accent_color(db_path, guild)


async def submit_pick(
    interaction: discord.Interaction,
    season: dict,
    user_id: int,
    week: int,
    team: str,
    *,
    edit: bool,
) -> None:
    """Shared placement path for the slash command and the panel selects."""
    bot = interaction.client
    db_path = bot.ctx.db_path  # type: ignore[attr-defined]
    now = discord.utils.utcnow().timestamp()

    def _q():
        with open_db(db_path) as conn:
            existing = logic.get_pick(conn, season["id"], user_id, week)
            game = logic.place_pick(conn, season, user_id, week, team, now)
            st = logic.player_status(conn, season, user_id, now)
            conn.commit()
        return game, st, existing is not None

    try:
        game, st, changed = await asyncio.to_thread(_q)
    except logic.PickError as exc:
        await _respond(interaction, content=f"❌ {str(exc)}", edit=edit)
        return
    assert st is not None  # the pick just landed, so the player exists
    color = await _accent(interaction, db_path, season["guild_id"])
    embed = build_pick_confirm_embed(game, st, changed=changed, color=color)
    await _respond(interaction, embed=embed, edit=edit)


async def _respond(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    edit: bool,
) -> None:
    kwargs: dict = {"content": content}
    if embed is not None:
        kwargs["embed"] = embed
    if edit:
        kwargs.setdefault("view", None)
        await interaction.response.edit_message(**kwargs)
    else:
        await interaction.response.send_message(**kwargs, ephemeral=True)


# ── joining ────────────────────────────────────────────────────────────


class JoinSeasonButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"survivor_join:(?P<season_id>\d+)",
):
    """The 🏈 Join button on the pinned season announcement (§2.2)."""

    def __init__(self, season_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="🏈 Join the Season",
                style=discord.ButtonStyle.success,
                custom_id=f"survivor_join:{season_id}",
            )
        )
        self.season_id = season_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> JoinSeasonButton:
        return cls(int(match["season_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        db_path = bot.ctx.db_path  # type: ignore[attr-defined]
        user_id = interaction.user.id

        now = discord.utils.utcnow().timestamp()

        def _q():
            with open_db(db_path) as conn:
                season = get_season(conn, self.season_id)
                if season is None or season["status"] == "complete":
                    return None, None, 0, None
                entered = conn.execute(
                    "SELECT 1 FROM survivor_players "
                    "WHERE season_id = ? AND user_id = ?",
                    (season["id"], user_id),
                ).fetchone()
                from bot_modules.services.economy_service import get_balance
                from bot_modules.survivor.gauntlet import compute_fate

                balance = get_balance(conn, season["guild_id"], user_id)
                fate = None
                if logic.elapsed_weeks(conn, season["season_year"], now):
                    if season["config"]["late_entry"] == "gauntlet":
                        fate = compute_fate(conn, season, now)
                return season, entered is not None, balance, fate

        season, entered, balance, fate = await asyncio.to_thread(_q)
        if season is None:
            await interaction.response.send_message(
                "This season has ended — the pin outlived it.", ephemeral=True
            )
            return
        if entered:
            await interaction.response.send_message(
                "You're already in. One entry per person — `/survivor status` "
                "for where you stand.",
                ephemeral=True,
            )
            return
        config = season["config"]
        buyin = int(config["buyin_coins"])

        # Season under way + gauntlet mode: the receipt flow (§4.2) — the
        # inherited fate shown before anyone pays.
        if fate is not None:
            from bot_modules.survivor.embeds import build_gauntlet_receipt_embed

            color = await _accent(interaction, db_path, season["guild_id"])
            total = fate.fee + buyin
            content = (
                "Your catch-up results are below — review before you pay. "
                f"Balance: **{balance:,}**"
                + (" ⚠️ (short)" if balance < total else "")
            )
            await interaction.response.send_message(
                content,
                embed=build_gauntlet_receipt_embed(fate, buyin=buyin, color=color),
                view=JoinConfirmView(self.season_id, gauntlet=True),
                ephemeral=True,
            )
            return
        lines = [
            "**The rules in one line:** pick one team to win each week, no "
            "team twice — lose and you're out.",
            f"Entry: **{buyin:,} coins**" if buyin else "Entry: **free**",
        ]
        if buyin:
            lines.append(f"Your balance: {balance:,}")
        await interaction.response.send_message(
            "\n".join(lines),
            view=JoinConfirmView(self.season_id),
            ephemeral=True,
        )


class JoinConfirmView(discord.ui.View):
    """The ephemeral confirm step behind the Join button. In gauntlet mode
    the fate is recomputed inside the transaction — the receipt was a
    preview; the moment of payment is the source of truth."""

    def __init__(self, season_id: int, *, gauntlet: bool = False) -> None:
        super().__init__(timeout=180)
        self.season_id = season_id
        self.gauntlet = gauntlet

    @discord.ui.button(label="✅ I'm In", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        bot = interaction.client
        db_path = bot.ctx.db_path  # type: ignore[attr-defined]
        user_id = interaction.user.id
        now = discord.utils.utcnow().timestamp()

        def _q():
            # BEGIN IMMEDIATE: this is a read-validate-then-write money path
            # (duplicate check → debit → entry row). Two double-clicked
            # confirms serialize on the write lock instead of both passing
            # the duplicate check and double-charging the buy-in.
            # No conn.commit() in here: open_db_immediate drives the
            # transaction by hand, so an inner commit leaves its exit-time
            # COMMIT with nothing to commit and raises — which is how the
            # first live join enrolled and charged the member but never
            # refreshed the panel, echoed, or granted the role (2026-08-18).
            with open_db_immediate(db_path) as conn:
                season = get_season(conn, self.season_id)
                if season is None:
                    raise logic.PickError("This season no longer exists.")
                elapsed = logic.elapsed_weeks(conn, season["season_year"], now)
                if elapsed:
                    from bot_modules.survivor.gauntlet import (
                        compute_fate,
                        execute_gauntlet_join,
                        ghost_only_join,
                    )

                    mode = season["config"]["late_entry"]
                    if mode == "closed":
                        raise logic.PickError(
                            "Enrollment closed at Week 1 kickoff this season."
                        )
                    if mode == "ghost_only":
                        ghost_only_join(conn, season, user_id, now)
                        return season, logic.JoinResult(charged=0), None
                    fate = compute_fate(conn, season, now)
                    execute_gauntlet_join(conn, season, user_id, fate, now)
                    charged = fate.fee + int(season["config"]["buyin_coins"])
                    return season, logic.JoinResult(charged=charged), fate
                result = logic.join_season(conn, season, user_id, now)
            return season, result, None

        try:
            season, result, fate = await asyncio.to_thread(_q)
        except logic.PickError as exc:
            await interaction.response.edit_message(
                content=f"❌ {str(exc)}", view=None
            )
            return

        # Respond FIRST: the member is charged and enrolled the moment the
        # commit lands, and a rate-limited role grant must not eat the
        # 3-second interaction window and turn a successful join into
        # "This interaction failed".
        charged = f" ({result.charged:,} coins paid)" if result.charged else ""
        if fate is not None and fate.dead:
            content = (
                f"The Gauntlet caught you at Week {fate.death_week}{charged}. "
                "You enter in the Ghost Streak side game — keep picking; the "
                "longest streak takes the side pot. 👻"
            )
        elif fate is not None:
            content = (
                f"You made it through the Gauntlet alive{charged}. "
                f"{len(fate.burned)} teams are already used — pick from "
                "the rest. 🏈"
            )
        else:
            content = (
                f"You're in{charged}. The weekly slate posts Wednesday — "
                "`/survivor pick` any time before kickoff. 🏈"
            )
        await interaction.response.edit_message(content=content, view=None)
        note = await _grant_survivor_role(interaction, season, fate=fate)
        if note:
            try:
                await interaction.followup.send(f"-# {note}", ephemeral=True)
            except discord.HTTPException:
                log.warning("survivor: join followup failed for %s", user_id)
        await refresh_panel(bot, db_path, season["id"])
        await _echo_join(bot, db_path, interaction, season["id"])


def join_echo_detail(entrants: int, pot: int) -> str:
    """The mini-advertisement line under a join echo — Survivor's own
    vocabulary, per Event Echo's frame-vs-voice split."""
    return (
        f"{entrants} players in · pot {pot:,} · one team a week, "
        "last one standing takes it"
    )


async def _echo_join(bot, db_path, interaction, season_id: int) -> None:
    """Fire the main-chat join echo (2026-08-18), best-effort. Event Echo
    owns the destination, cooldowns, and one-per-member dedupe; a guild
    with no echo channel configured simply never echoes."""
    guild = interaction.guild
    if guild is None:
        return

    def _q():
        with open_db(db_path) as conn:
            season = get_season(conn, season_id)
            if season is None:
                return None
            entrants = conn.execute(
                "SELECT COUNT(*) FROM survivor_players WHERE season_id = ?",
                (season_id,),
            ).fetchone()[0]
            pot = logic.pot_totals(conn, season)["main"]
        return season, int(entrants), pot

    loaded = await asyncio.to_thread(_q)
    if loaded is None:
        return
    season, entrants, pot = loaded
    config = season["config"]
    channel_id = int(
        config.get("announcement_channel_id") or config["channel_id"] or 0
    )
    message_id = int(config.get("announcement_message_id") or 0)
    if not channel_id or not message_id:
        return  # no panel to land on — no ad without a door
    url = (
        f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
    )
    member = interaction.user
    name = (
        member.display_name if isinstance(member, discord.Member)
        else member.name
    )
    try:
        from bot_modules.services.event_echo_service import echo_survivor_join

        await echo_survivor_join(
            bot, guild,
            member_name=name,
            season_id=season_id,
            user_id=member.id,
            detail=join_echo_detail(entrants, pot),
            url=url,
        )
    except Exception:
        log.exception("survivor: join echo failed")


async def _grant_survivor_role(
    interaction: discord.Interaction, season: dict, *, fate=None
) -> str | None:
    """Grant the arrival role: 👻 Ghost for the dead-on-arrival (role swap at
    death, §1.7 as decided), 🏈 Survivor for the living."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return None
    dead = fate is not None and fate.dead
    key = "role_ghost_id" if dead else "role_survivor_id"
    role = guild.get_role(int(season["config"][key] or 0))
    if role is None:
        return "role not configured — an admin can grant it later"
    try:
        await member.add_roles(role, reason="Survivor: joined the season")
    except (discord.Forbidden, discord.HTTPException):
        log.exception("survivor: role grant failed for %s", member.id)
        return "couldn't grant the role — an admin will sort it"
    return None


class SlatePickButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"survivor_slate:(?P<season_id>\d+)",
):
    """The [🏈 Make your pick] button on the Wednesday slate (§2.3) — opens
    the same ephemeral AFC/NFC panel as bare /survivor pick, so casuals never
    touch slash syntax. Persistent across restarts like the Join button."""

    def __init__(self, season_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="🏈 Make Your Pick",
                style=discord.ButtonStyle.primary,
                custom_id=f"survivor_slate:{season_id}",
            )
        )
        self.season_id = season_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> SlatePickButton:
        return cls(int(match["season_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        db_path = bot.ctx.db_path  # type: ignore[attr-defined]
        user_id = interaction.user.id
        now = discord.utils.utcnow().timestamp()

        def _q():
            with open_db(db_path) as conn:
                season = get_season(conn, self.season_id)
                if season is None or season["status"] == "complete":
                    return None, None, [], 0.0
                week = logic.pick_week(conn, season["season_year"], now)
                games = (
                    logic.legal_teams(conn, season, user_id, week, now)
                    if week is not None else []
                )
                from bot_modules.core.db_utils import get_tz_offset_hours

                offset = get_tz_offset_hours(conn, season["guild_id"])
            return season, week, games, offset

        season, week, games, offset = await asyncio.to_thread(_q)
        if season is None or week is None:
            await interaction.response.send_message(
                "No games left to pick right now.",
                ephemeral=True,
            )
            return
        if not games:
            await interaction.response.send_message(
                "No eligible team left this week — everything still to play "
                "is already used. You survive the week.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Week {week} — pick a team to **win**. Locks at that game's "
            "kickoff; hidden until the results post.",
            view=PickPanel(bot, season, user_id, week, games, offset),  # pyright: ignore[reportArgumentType]
            ephemeral=True,
        )


class HistoryButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"survivor_history:(?P<season_id>\d+)",
):
    """📜 My History on the channel panel — ephemeral and personal, so it
    shows the clicker their FULL history including the current week's
    still-secret pick (tagged as hidden from others). The public
    /survivor history remains revealed-only; both render through one
    builder so the faces can't drift."""

    def __init__(self, season_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="📜 My History",
                style=discord.ButtonStyle.secondary,
                custom_id=f"survivor_history:{season_id}",
            )
        )
        self.season_id = season_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> HistoryButton:
        return cls(int(match["season_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        db_path = bot.ctx.db_path  # type: ignore[attr-defined]
        user_id = interaction.user.id

        def _q():
            with open_db(db_path) as conn:
                season = get_season(conn, self.season_id)
                if season is None:
                    return None, [], 0
                rows = logic.history_rows(conn, season, user_id)
                revealed = int(
                    season["config"].get("last_reckoned_week") or 0
                )
            return season, rows, revealed

        season, rows, revealed = await asyncio.to_thread(_q)
        if season is None:
            await interaction.response.send_message(
                "This season no longer exists.", ephemeral=True
            )
            return
        if not rows:
            await interaction.response.send_message(
                "No picks on record yet — your history starts with your "
                "first pick. `/survivor pick`",
                ephemeral=True,
            )
            return
        from bot_modules.survivor.embeds import build_history_embed

        color = await _accent(interaction, db_path, season["guild_id"])
        name = (
            interaction.user.display_name
            if isinstance(interaction.user, discord.Member)
            else interaction.user.name
        )
        await interaction.response.send_message(
            embed=build_history_embed(
                rows,
                display_name=discord.utils.escape_markdown(name),
                revealed_week=revealed,
                own=True,
                color=color,
            ),
            ephemeral=True,
        )


async def swap_member_roles(
    bot, guild_id: int, config: dict, user_id: int, *, to_ghost: bool
) -> str | None:
    """Best-effort Survivor↔Ghost swap (§1.7 as decided) — shared by the
    Reckoning's death march, the admin roster buttons, and any future path.
    Returns a note when skipped or failed; never raises."""
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        return "role swap skipped — bot offline"
    member = guild.get_member(user_id)
    if member is None:
        return "role swap skipped — member not in guild"
    survivor = guild.get_role(int(config.get("role_survivor_id") or 0))
    ghost = guild.get_role(int(config.get("role_ghost_id") or 0))
    add, remove = (ghost, survivor) if to_ghost else (survivor, ghost)
    if add is None and remove is None:
        return "role swap skipped — roles not configured"
    try:
        if remove is not None and remove in member.roles:
            await member.remove_roles(remove, reason="Survivor: life-state change")
        if add is not None and add not in member.roles:
            await member.add_roles(add, reason="Survivor: life-state change")
    except (discord.Forbidden, discord.HTTPException):
        log.exception("survivor: role swap failed for %s", user_id)
        return "role swap failed — check Manage Roles"
    return None


class PanelError(RuntimeError):
    """A panel post/repost couldn't happen; message is presentable."""


async def build_live_panel(
    bot, db_path, season_id: int
) -> tuple[dict, discord.Embed, bool] | None:
    """The single source of the channel panel: season pitch, current week's
    games, standings line, joining door — assembled from live data. Returns
    (season, embed, join_open); None when season/guild is gone. Shared by
    the dashboard post route, the Wednesday repost, and every in-place edit,
    so the panel can never fork content by path."""
    now = discord.utils.utcnow().timestamp()

    def _q():
        with open_db(db_path) as conn:
            season = get_season(conn, season_id)
            if season is None:
                return None
            year = season["season_year"]
            week = logic.pick_week(conn, year, now)
            games = [
                {
                    "home": r["home"], "away": r["away"],
                    "kickoff_ts": logic.kickoff_ts(r["kickoff_utc"]),
                }
                for r in conn.execute(
                    "SELECT home, away, kickoff_utc FROM nfl_games "
                    "WHERE season_year = ? AND week = ? AND status != 'postponed' "
                    "ORDER BY kickoff_utc",
                    (year, week if week is not None else -1),
                ).fetchall()
            ]
            counts = conn.execute(
                "SELECT "
                " SUM(CASE WHEN status = 'alive' THEN 1 ELSE 0 END) AS alive,"
                " SUM(CASE WHEN status = 'ghost' THEN 1 ELSE 0 END) AS ghost,"
                " COUNT(*) AS total "
                "FROM survivor_players WHERE season_id = ?",
                (season_id,),
            ).fetchone()
            picked = conn.execute(
                "SELECT COUNT(DISTINCT p.user_id) FROM survivor_picks p "
                "JOIN survivor_players pl ON pl.season_id = p.season_id "
                "AND pl.user_id = p.user_id "
                "WHERE p.season_id = ? AND p.week = ? AND pl.status = 'alive'",
                (season_id, week if week is not None else -1),
            ).fetchone()[0]
            pots = logic.pot_totals(conn, season)
            gauntlet_mode = bool(logic.elapsed_weeks(conn, year, now))
            roster = [
                (int(r["user_id"]), r["status"])
                for r in conn.execute(
                    "SELECT user_id, status FROM survivor_players "
                    "WHERE season_id = ?",
                    (season_id,),
                ).fetchall()
            ]
        return (
            season, week, games, int(counts["alive"] or 0),
            int(counts["ghost"] or 0), int(counts["total"] or 0),
            int(picked), pots, gauntlet_mode, roster,
        )

    loaded = await asyncio.to_thread(_q)
    if loaded is None:
        return None
    (season, week, games, alive, ghost, total, picked, pots,
     gauntlet_mode, roster) = loaded
    guild = bot.get_guild(season["guild_id"])
    if guild is None:
        return None
    config = season["config"]
    from bot_modules.survivor.reckoning import slate_join_line

    join_open = slate_join_line(
        buyin=int(config["buyin_coins"]),
        late_entry=str(config["late_entry"]),
        gauntlet_mode=gauntlet_mode,
    ) is not None
    color = await branding.resolve_accent_color(db_path, guild)

    def _display(user_id: int) -> str:
        member = guild.get_member(user_id)
        return (
            discord.utils.escape_markdown(member.display_name)
            if member else f"soul {user_id}"
        )

    # Sorted by name so the list is scannable and stable between edits —
    # join order would reshuffle nothing but still read as arbitrary.
    alive_names = sorted(
        (_display(uid) for uid, st in roster if st == "alive"), key=str.casefold
    )
    eliminated_names = sorted(
        (_display(uid) for uid, st in roster if st != "alive"), key=str.casefold
    )
    # Enrolling seasons show the pre-kickoff face (week=None hides the
    # slate section even if the schedule is loaded but week 1 is far off?
    # No — the slate section shows as soon as a pick week exists; enrolling
    # status just means week 1 hasn't kicked yet, which is exactly when
    # members want the games list. week is None only with no open games.
    embed = build_panel_embed(
        season_name=season["name"],
        entrants=total,
        buyin=int(config["buyin_coins"]),
        gauntlet_mode=gauntlet_mode,
        late_entry=str(config["late_entry"]),
        strikes=int(config["strikes"]),
        week=week,
        games=games,
        alive=alive,
        eliminated=ghost,
        picked=picked,
        pot=pots["main"],
        ghost_pot=pots["ghost"],
        alive_names=alive_names,
        eliminated_names=eliminated_names,
        color=color,
    )
    return season, embed, join_open


def panel_view(season_id: int, *, join_open: bool) -> discord.ui.View:
    """The panel's buttons — persistent DynamicItems, so every past copy of
    the panel keeps working across restarts."""
    view = discord.ui.View(timeout=None)
    view.add_item(SlatePickButton(season_id))
    if join_open:
        view.add_item(JoinSeasonButton(season_id))
    view.add_item(HistoryButton(season_id))
    return view


async def refresh_panel(bot, db_path, season_id: int) -> None:
    """Best-effort in-place edit of the pinned panel (joins, settles)."""
    built = await build_live_panel(bot, db_path, season_id)
    if built is None:
        return
    season, embed, join_open = built
    config = season["config"]
    channel_id = int(
        config.get("announcement_channel_id") or config["channel_id"] or 0
    )
    message_id = int(config.get("announcement_message_id") or 0)
    if not channel_id or not message_id:
        return
    guild = bot.get_guild(season["guild_id"])
    channel = guild.get_channel(channel_id) if guild else None
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.get_partial_message(message_id).edit(
            embed=embed, view=panel_view(season["id"], join_open=join_open)
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("survivor: panel refresh failed for %s", message_id)


async def repost_panel(
    bot,
    db_path,
    season_id: int,
    *,
    content: str | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> tuple[discord.Message, bool, bool]:
    """Post the panel fresh at the channel bottom, pin it, retire the
    previous copy, and store the new ids. The Wednesday week-open repost
    passes ping ``content``; the dashboard post passes none. Returns
    (message, pinned, retired_previous); raises PanelError when it can't.
    """
    built = await build_live_panel(bot, db_path, season_id)
    if built is None:
        raise PanelError("Bot offline or the season/guild is gone.")
    season, embed, join_open = built
    config = season["config"]
    guild = bot.get_guild(season["guild_id"])
    channel = (
        guild.get_channel(int(config["channel_id"] or 0)) if guild else None
    )
    if not isinstance(channel, discord.TextChannel):
        raise PanelError(
            "The configured channel isn't a text channel the bot can see."
        )
    try:
        message = await channel.send(
            content=content,
            embed=embed,
            view=panel_view(season["id"], join_open=join_open),
            allowed_mentions=allowed_mentions or discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        raise PanelError(f"Couldn't post the panel: {exc}") from exc
    try:
        await message.pin(reason="Survivor channel panel")
        pinned = True
        # The weekly repost would otherwise leave a "pinned a message"
        # system notice every Wednesday — sweep it, best-effort.
        try:
            async for recent in channel.history(limit=5):
                if (
                    recent.type == discord.MessageType.pins_add
                    and recent.author.id == getattr(bot.user, "id", 0)
                ):
                    await recent.delete()
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
    except (discord.Forbidden, discord.HTTPException):
        pinned = False

    # Retire the previous copy in the channel it actually lives in.
    old_message_id = int(config.get("announcement_message_id") or 0)
    old_channel_id = int(
        config.get("announcement_channel_id") or 0
    ) or int(config["channel_id"] or 0)
    retired = False
    if old_message_id:
        old_channel = guild.get_channel(old_channel_id) if guild else None
        if isinstance(old_channel, discord.TextChannel):
            try:
                await old_channel.get_partial_message(old_message_id).delete()
                retired = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning(
                    "survivor: couldn't retire old panel %s", old_message_id
                )

    def _store():
        with open_db(db_path) as conn:
            from bot_modules.services.survivor_service import update_config

            update_config(conn, season["id"], {
                "announcement_message_id": message.id,
                "announcement_channel_id": channel.id,
            })
            conn.commit()

    await asyncio.to_thread(_store)
    return message, pinned, retired
