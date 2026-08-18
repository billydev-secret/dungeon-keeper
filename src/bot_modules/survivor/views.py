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

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import open_db, open_db_immediate
from bot_modules.services.survivor_service import get_season
from bot_modules.survivor import logic
from bot_modules.survivor.embeds import (
    build_announcement_embed,
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
                placeholder=f"{label} — pick a team to win",
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
    assert interaction.guild is not None
    color = await resolve_accent_color(db_path, interaction.guild)
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
    """The 🌾 Join button on the pinned season announcement (§2.2)."""

    def __init__(self, season_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="🌾 Join the Season",
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

        def _q():
            with open_db(db_path) as conn:
                season = get_season(conn, self.season_id)
                if season is None or season["status"] == "complete":
                    return None, None, 0
                entered = conn.execute(
                    "SELECT 1 FROM survivor_players "
                    "WHERE season_id = ? AND user_id = ?",
                    (season["id"], user_id),
                ).fetchone()
                from bot_modules.services.economy_service import get_balance

                balance = get_balance(conn, season["guild_id"], user_id)
                return season, entered is not None, balance

        season, entered, balance = await asyncio.to_thread(_q)
        if season is None:
            await interaction.response.send_message(
                "This season has ended — the pin outlived it.", ephemeral=True
            )
            return
        if entered:
            await interaction.response.send_message(
                "You're already in. One entry per person — `/survivor status` "
                "for where you stand. 🌾",
                ephemeral=True,
            )
            return
        buyin = int(season["config"]["buyin_coins"])
        lines = [
            "**one sentence of rules:** pick one team to win each week, no "
            "team twice — your team loses, you're out.",
            f"entry: **{buyin:,} coins**" if buyin else "entry: **free**",
        ]
        if buyin:
            lines.append(f"your balance: {balance:,}")
        await interaction.response.send_message(
            "\n".join(lines),
            view=JoinConfirmView(self.season_id),
            ephemeral=True,
        )


class JoinConfirmView(discord.ui.View):
    """The ephemeral confirm step behind the Join button."""

    def __init__(self, season_id: int) -> None:
        super().__init__(timeout=180)
        self.season_id = season_id

    @discord.ui.button(label="🌾 I'm in", style=discord.ButtonStyle.success)
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
            with open_db_immediate(db_path) as conn:
                season = get_season(conn, self.season_id)
                if season is None:
                    raise logic.PickError("This season no longer exists.")
                result = logic.join_season(conn, season, user_id, now)
                conn.commit()
            return season, result

        try:
            season, result = await asyncio.to_thread(_q)
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
        await interaction.response.edit_message(
            content=(
                f"you're in{charged}. the slate posts Wednesdays; "
                "your first pick awaits. 🌾"
            ),
            view=None,
        )
        note = await _grant_survivor_role(interaction, season)
        if note:
            try:
                await interaction.followup.send(f"-# {note}", ephemeral=True)
            except discord.HTTPException:
                log.warning("survivor: join followup failed for %s", user_id)
        await refresh_announcement(bot, db_path, season["id"])


async def _grant_survivor_role(
    interaction: discord.Interaction, season: dict
) -> str | None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return None
    role = guild.get_role(int(season["config"]["role_survivor_id"] or 0))
    if role is None:
        return "role not configured — an admin can grant it later"
    try:
        await member.add_roles(role, reason="Survivor: joined the season")
    except (discord.Forbidden, discord.HTTPException):
        log.exception("survivor: role grant failed for %s", member.id)
        return "couldn't grant the role — an admin will sort it"
    return None


async def build_live_announcement(
    bot, db_path, season_id: int
) -> tuple[dict, discord.Embed] | None:
    """The single source of the announcement payload — live entrant count,
    gauntlet mode, accent color. Shared by the dashboard post/repost route
    and the post-join counter refresh, so the pinned message can never flip
    content depending on which path touched it last."""

    def _q():
        with open_db(db_path) as conn:
            season = get_season(conn, season_id)
            if season is None:
                return None, 0, []
            entrants = conn.execute(
                "SELECT COUNT(*) FROM survivor_players WHERE season_id = ?",
                (season_id,),
            ).fetchone()[0]
            elapsed = logic.elapsed_weeks(
                conn, season["season_year"], discord.utils.utcnow().timestamp()
            )
        return season, int(entrants), elapsed

    season, entrants, elapsed = await asyncio.to_thread(_q)
    if season is None:
        return None
    guild = bot.get_guild(season["guild_id"])
    if guild is None:
        return None
    color = await resolve_accent_color(db_path, guild)
    embed = build_announcement_embed(
        season_name=season["name"],
        entrants=entrants,
        buyin=int(season["config"]["buyin_coins"]),
        gauntlet_mode=bool(elapsed),
        late_entry=str(season["config"]["late_entry"]),
        strikes=int(season["config"]["strikes"]),
        color=color,
    )
    return season, embed


async def refresh_announcement(bot, db_path, season_id: int) -> None:
    """Best-effort entrant-counter refresh on the pinned announcement."""
    built = await build_live_announcement(bot, db_path, season_id)
    if built is None:
        return
    season, embed = built
    config = season["config"]
    # The channel the pin was POSTED to, not the (re-pointable) current
    # Survivor channel — the route's retire path already resolves this way,
    # and diverging froze the counter after a re-point (stage-4 review).
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
        message = channel.get_partial_message(message_id)
        await message.edit(embed=embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("survivor: announcement refresh failed for %s", message_id)
