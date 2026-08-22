"""``/info`` — a member's own card, and the buttons to change what's on it.

The member-facing counterpart to ``/modinfo``. It gathers state, hands it to
``bot_modules.member_info.logic`` to decide what to show and offer, and sends
one ephemeral panel.

Everything it reads is either the caller's own data or guild configuration.
There is no member argument, deliberately: a lookup of *someone else* would
have to consult the no-contact list and reason about every field's leak
surface (``docs/no_contact_spec.md``), and none of that is needed for a card
you can only point at yourself.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.member_info.embeds import build_member_info_embed
from bot_modules.member_info.logic import (
    STATE_IN,
    STATE_OUT,
    STATE_UNSET,
    AccountFacts,
    FeatureState,
    build_optin_rows,
    visible_top_channels,
)
from bot_modules.member_info.views import MemberInfoView
from bot_modules.services.activity_graphs import (
    query_message_activity,
    query_xp_activity_with_breakdown,
    render_activity_chart,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

_DAY_SECONDS = 86400
_ACTIVITY_WINDOW_DAYS = 30
_CHART_FILENAME = "info_activity.png"


def _feature_states(conn, guild_id: int, member: discord.Member, bot) -> dict[str, FeatureState]:
    """Read every opt-in's guild config and this member's state in it.

    Runs inside the worker thread with one connection. Each feature's own
    helpers are reused rather than re-querying its tables here — a second copy
    of "is this member opted out?" is exactly the drift that
    ``no_contact_service`` was created to end.

    Each block is independently fallible. Reusing seven features' internals
    means seven chances for a helper signature to drift or a table to be
    absent (wellness's are created by the web server's startup, not by a
    migration), and one of those must cost its own row, not the whole card.
    The alternative is worse than it sounds: this runs after ``defer()``, and
    the tree's error handler only speaks when the interaction is unanswered,
    so an escaping exception leaves the member on "thinking…" forever.
    """
    # Every import is lazy *and* inside its own feature's guard. Lazy alone
    # was not enough: an ImportError raised at call time from any one of these
    # modules escaped `_feature_states` entirely and cost the whole card — the
    # exact outcome the per-feature guarding exists to prevent.
    role_ids = {r.id for r in member.roles}
    states: dict[str, FeatureState] = {}

    try:
        from bot_modules.cogs.pen_pals_cog import (  # noqa: PLC0415
            _get_active_session,
            _get_config as _pen_pals_config,
            _in_pool,
            _is_opted_out,
        )
        # ── Pen Pals ─────────────────────────────────────────────────────────
        cfg = _pen_pals_config(conn, guild_id)
        if cfg is not None and cfg["enabled"]:
            if _is_opted_out(conn, guild_id, member.id):
                state = STATE_OUT
            elif _in_pool(conn, guild_id, member.id) or _get_active_session(
                conn, guild_id, member.id
            ):
                state = STATE_IN
            else:
                state = STATE_UNSET
            # An opt-in role the member lacks means Join would be refused — show
            # the status, offer no button that cannot work. Mirror `_handle_join`
            # exactly, including its deleted-role case: it only refuses when the
            # role still resolves, so a config pointing at a deleted role lets
            # everyone join and must not hide the button from everyone.
            gate = int(cfg["opt_in_role_id"] or 0)
            gate_blocks = (
                gate != 0
                and gate not in role_ids
                and member.guild.get_role(gate) is not None
            )
            states["pen_pals"] = FeatureState(
                configured=True,
                state=state,
                actionable=not gate_blocks and bot.get_cog("PenPalsCog") is not None,
            )

    except Exception:
        log.exception("info panel: reading %s state failed", "pen_pals")
    try:
        from bot_modules.services.whisper_repo import (  # noqa: PLC0415
            get_whisper_config,
        )
        # ── Whispers ─────────────────────────────────────────────────────────
        whisper_cfg = get_whisper_config(conn, guild_id)
        if whisper_cfg.role_id:
            states["whispers"] = FeatureState(
                configured=True,
                state=STATE_IN if whisper_cfg.role_id in role_ids else STATE_UNSET,
                actionable=bot.get_cog("WhisperCog") is not None,
            )

    except Exception:
        log.exception("info panel: reading %s state failed", "whispers")
    try:
        from bot_modules.services.guess_repo import get_guess_config  # noqa: PLC0415
        # ── Guess pool ───────────────────────────────────────────────────────
        guess_cfg = get_guess_config(conn, guild_id)
        if guess_cfg.guess_role_id:
            states["guess"] = FeatureState(
                configured=True,
                state=STATE_IN if guess_cfg.guess_role_id in role_ids else STATE_UNSET,
                actionable=bot.get_cog("GuessCog") is not None,
            )

    except Exception:
        log.exception("info panel: reading %s state failed", "guess")
    try:
        from bot_modules.services.dm_perms_service import (  # noqa: PLC0415
            get_dm_mode_role_ids_with_conn,
            resolve_mode,
        )
        # ── DM mode ──────────────────────────────────────────────────────────
        # Gate on the cog, not on configured role ids: with no `dm_mode_roles` row
        # the ids are all 0, but the feature still works — `resolve_mode` falls back
        # to the default role *names* and `ensure_dm_roles` creates them on demand.
        # Gating on the ids hid the row (and a button that would have worked) on
        # every guild that never set explicit ones.
        if bot.get_cog("DmPermsCog") is not None:
            dm_roles = get_dm_mode_role_ids_with_conn(conn, guild_id)
            mode = resolve_mode(member, dm_roles)
            states["dm_mode"] = FeatureState(
                configured=True,
                state=STATE_IN,
                detail=f"Currently **{mode}**",
            )

    except Exception:
        log.exception("info panel: reading %s state failed", "dm_mode")
    try:
        from bot_modules.services.wellness_service import (  # noqa: PLC0415
            get_wellness_config,
            get_wellness_user,
        )
        # ── Wellness ─────────────────────────────────────────────────────────
        wellness_cfg = get_wellness_config(conn, guild_id)
        if wellness_cfg is not None and wellness_cfg.role_id:
            row = get_wellness_user(conn, guild_id, member.id)
            if row is None:
                state = STATE_UNSET
            else:
                state = STATE_IN if row.is_active else STATE_OUT
            states["wellness"] = FeatureState(
                configured=True,
                state=state,
                actionable=bot.get_cog("WellnessCog") is not None,
            )

    except Exception:
        log.exception("info panel: reading %s state failed", "wellness")
    try:
        from bot_modules.services.birthday_service import (  # noqa: PLC0415
            has_birthday,
        )
        # ── Birthday ─────────────────────────────────────────────────────────
        if bot.get_cog("BirthdayCog") is not None:
            states["birthday"] = FeatureState(
                configured=True,
                state=STATE_IN if has_birthday(conn, guild_id, member.id) else STATE_UNSET,
            )

    except Exception:
        log.exception("info panel: reading %s state failed", "birthday")
    try:
        # ── No-contact ───────────────────────────────────────────────────────
        if bot.get_cog("NoContactCog") is not None:
            states["no_contact"] = FeatureState(configured=True, state=STATE_UNSET)

    except Exception:
        log.exception("info panel: reading %s state failed", "no_contact")
    return states


def _viewable_channel_ids(guild: discord.Guild, member: discord.Member) -> set[int]:
    """Every channel *and thread* the member can currently open.

    Threads are not optional here. ``award_message_xp`` accepts a
    ``discord.Thread`` and stores ``message.channel.id``, so
    ``processed_messages`` holds rows keyed by *thread* id — while
    ``guild.channels`` excludes threads entirely. Filtering against channels
    alone silently drops every thread row, and a member who talks mostly in
    threads reads "No messages recorded in the last 30 days" directly beneath
    a header counting those very messages.

    ``Thread.permissions_for`` delegates to the parent channel, so one check
    covers both kinds.

    This still cannot see **archived** threads: discord.py drops a thread from
    the guild cache as soon as it archives (``state.parse_thread_update`` ->
    ``guild._remove_thread``), Discord archives after 24h-1 week of quiet, and
    ``processed_messages`` stores the thread id with no parent to fall back to.
    Those rows are excluded rather than shown unchecked — visibility we cannot
    verify is not visibility. ``_activity_value`` says so plainly instead of
    claiming the member posted nothing.
    """
    return {
        channel.id
        for channel in (*guild.channels, *guild.threads)
        if channel.permissions_for(member).view_channel
    }


class MemberInfoCog(commands.Cog):
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    @app_commands.command(
        name="info",
        description="Your own info — activity, level, opt-ins and wallet.",
    )
    @app_commands.guild_only()
    async def info_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "❌ Run this from inside a server, not a DM.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self._render(interaction, guild, member)
        except Exception:
            # Past the defer, the tree's error handler is mute: it only speaks
            # when the interaction is unanswered (events_cog._on_tree_error).
            # Without this the member is left on "thinking…" with no error and
            # no card, which reads as the bot being broken rather than one
            # section failing.
            log.exception("info panel failed for %s in %s", member.id, guild.id)
            await interaction.followup.send(
                "❌ Couldn't build your info card just now. Try again in a moment.",
                ephemeral=True,
            )

    async def _render(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        member: discord.Member,
    ) -> None:
        ctx = self.bot.ctx
        guild_id = guild.id
        user_id = member.id
        since = datetime.now(timezone.utc).timestamp() - _ACTIVITY_WINDOW_DAYS * _DAY_SECONDS

        def _fetch() -> dict[str, Any]:
            from bot_modules.core.xp_system import (  # noqa: PLC0415
                load_xp_settings,
                xp_required_for_level,
            )
            from bot_modules.services.economy_service import (  # noqa: PLC0415
                get_balance,
                get_streak_shields,
                get_streak_summary,
                load_econ_settings,
            )
            from bot_modules.services.economy_rentals_service import (  # noqa: PLC0415
                list_member_rentals,
            )

            with ctx.open_db() as conn:
                xp_row = conn.execute(
                    "SELECT total_xp, level FROM member_xp "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                xp_by_source = dict(
                    conn.execute(
                        "SELECT source, SUM(amount) FROM xp_events "
                        "WHERE guild_id = ? AND user_id = ? GROUP BY source",
                        (guild_id, user_id),
                    ).fetchall()
                )
                last_ts = conn.execute(
                    "SELECT MAX(created_at) FROM processed_messages "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()[0]
                # Over-fetch: the visibility filter below drops channels the
                # member can no longer see, and a strict LIMIT 3 here could
                # leave the field empty after filtering.
                top_channels = conn.execute(
                    """
                    SELECT channel_id, COUNT(*) AS cnt
                    FROM processed_messages
                    WHERE guild_id = ? AND user_id = ? AND created_at >= ?
                    GROUP BY channel_id ORDER BY cnt DESC LIMIT 25
                    """,
                    (guild_id, user_id, since),
                ).fetchall()

                tz_off = get_tz_offset_hours(conn, guild_id)
                _, msg_counts, _ = query_message_activity(
                    conn, guild_id, "day", user_id=user_id, utc_offset_hours=tz_off
                )
                xp_labels, xp_totals, _, xp_series = query_xp_activity_with_breakdown(
                    conn, guild_id, "day", user_id=user_id, utc_offset_hours=tz_off
                )

                # The curve factor is a live guild dial, so the next-level
                # threshold is read per guild rather than assumed.
                next_level_xp = (
                    xp_required_for_level(
                        int(xp_row["level"]) + 1, load_xp_settings(conn, guild_id)
                    )
                    if xp_row
                    else None
                )

                econ = load_econ_settings(conn, guild_id)
                if econ.enabled:
                    balance = get_balance(conn, guild_id, user_id)
                    rentals = list_member_rentals(conn, guild_id, user_id)
                    shields = get_streak_shields(conn, guild_id, user_id)
                    streak, longest_streak = get_streak_summary(
                        conn, guild_id, user_id
                    )
                else:
                    balance, rentals, shields = 0, [], 0
                    streak, longest_streak = 0, 0

                assistant_name = ""
                if self.bot.get_cog("AdvisorCog") is not None:
                    # Per-guild branding — never hardcode the default name.
                    from bot_modules.services.branding_service import (  # noqa: PLC0415
                        resolve_assistant_name_conn,
                    )

                    try:
                        assistant_name = resolve_assistant_name_conn(conn, guild_id)
                    except Exception:
                        log.exception("info panel: assistant name lookup failed")

                states = _feature_states(conn, guild_id, member, self.bot)

            return {
                "xp_row": xp_row,
                "xp_by_source": xp_by_source,
                "last_ts": last_ts,
                "top_channels": top_channels,
                "msgs_30d": sum(msg_counts),
                "xp_labels": xp_labels,
                "xp_totals": xp_totals,
                "xp_series": xp_series,
                "econ": econ,
                "balance": balance,
                "rentals": rentals,
                "shields": shields,
                "streak": streak,
                "longest_streak": longest_streak,
                "next_level_xp": next_level_xp,
                "assistant_name": assistant_name,
                "states": states,
            }

        data = await asyncio.to_thread(_fetch)

        viewable = _viewable_channel_ids(guild, member)
        xp_row = data["xp_row"]
        joined_at = member.joined_at
        facts = AccountFacts(
            account_age_days=(datetime.now(timezone.utc) - member.created_at).days,
            created_ts=int(member.created_at.timestamp()),
            joined_ts=int(joined_at.timestamp()) if joined_at else None,
            role_names=[r.name for r in reversed(member.roles) if not r.is_default()],
            level=xp_row["level"] if xp_row else None,
            total_xp=float(xp_row["total_xp"]) if xp_row else 0.0,
            xp_by_source=data["xp_by_source"],
            msgs_30d=data["msgs_30d"],
            top_channels=visible_top_channels(data["top_channels"], viewable),
            last_seen_ts=data["last_ts"],
            next_level_xp=data["next_level_xp"],
            current_streak=data["streak"],
            longest_streak=data["longest_streak"],
        )

        wallet_line, wallet_extra = self._wallet_summary(data, user_id)
        rows = build_optin_rows(data["states"])
        accent = await safe_resolve_accent(ctx, guild, log_label="member info")

        chart_bytes = await asyncio.to_thread(
            render_activity_chart,
            data["xp_labels"],
            data["xp_totals"],
            [],
            "Your last 30 days — XP by source",
            "day",
            show_members=False,
            y_label="XP",
            bar_label="XP",
            by_source=data["xp_series"],
        )

        embed = build_member_info_embed(
            display_name=member.display_name,
            avatar_url=member.display_avatar.url if member.display_avatar else None,
            facts=facts,
            optin_rows=rows,
            wallet_line=wallet_line,
            wallet_extra=wallet_extra,
            help_lines=self._help_lines(data["assistant_name"]),
            color=accent,
        )
        await interaction.followup.send(
            embed=embed,
            file=discord.File(io.BytesIO(chart_bytes), filename=_CHART_FILENAME),
            view=MemberInfoView(self.bot, rows),
            ephemeral=True,
        )

    def _help_lines(self, assistant_name: str) -> list[str]:
        """Where to go next: the assistant, and the member's own data.

        An info card is exactly where someone wonders "what does this bot know
        about me?", so the erasure command is named here rather than left to be
        found. Each line is gated on its cog being loaded — the same rule the
        opt-in rows follow: never name a command this server does not run.
        """
        lines: list[str] = []
        if assistant_name:
            lines.append(
                f"`/ask` — ask {assistant_name} how anything here works."
            )
        if self.bot.get_cog("PrivacyCog") is not None:
            lines.append(
                "`/delete_me` — erase your messages. "
                "The Help guide's **Your Data & Privacy** lists everything stored."
            )
        return lines

    def _wallet_summary(
        self, data: dict[str, Any], user_id: int
    ) -> tuple[str, list[str]]:
        """A one-line balance plus live rentals, or nothing at all.

        A guild with the economy switched off gets no wallet section — showing
        a zero balance in a currency that does not exist here would invent a
        feature rather than report one.
        """
        from bot_modules.economy.view_helpers import unit  # noqa: PLC0415
        from bot_modules.economy.wallet import rental_lines  # noqa: PLC0415

        econ = data["econ"]
        if not econ.enabled:
            return "", []

        balance = data["balance"]
        line = f"{econ.currency_emoji} **{balance:,}** {unit(econ, balance)}"
        if data["streak"]:
            line += f" · 🔥 {data['streak']:,}-day streak"
            if data["longest_streak"] > data["streak"]:
                line += f" (best: {data['longest_streak']:,})"
        if data["shields"]:
            line += " · 🛡️ streak shield held"
        # The viewer id is what renders gift attribution ("gift received" /
        # "gift to @x") — passing a placeholder here would quietly drop it.
        extra = rental_lines(econ, data["rentals"], user_id)
        extra.append("`/bank wallet` for your full ledger and casino record.")
        return line, extra


async def setup(bot: "Bot") -> None:
    await bot.add_cog(MemberInfoCog(bot))
