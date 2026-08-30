"""XP commands."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.services import xp_rollup_service
from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, safe_resolve_accent
from bot_modules.services.xp_service import handle_level_progress, nsfw_grant_role_id
from bot_modules.core.xp_system import (
    XP_SOURCE_GRANT,
    XP_SOURCE_IMAGE_REACT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    apply_xp_award,
    get_user_xp_standing,
    get_xp_distribution_stats,
    get_xp_leaderboard,
    has_any_member_xp,
    has_any_xp_events,
)
from bot_modules.services.replies import NO_PERMISSION

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

# Days rolled up per pass. The first run after this ships has ~180 days of
# history to cover; capping the pass keeps that one write from being long
# enough to matter, and the loop catches up over the following days.
_ROLLUP_DAYS_PER_PASS = 40

# Raw rows deleted per guild per pass once retention is on. The busy
# guild has ~500k to clear on the first run; nibbling keeps that off the
# write lock that XP awards need, at the cost of taking a few days.
_PRUNE_ROWS_PER_PASS = xp_rollup_service.PRUNE_CHUNK


async def _collect_backfill_channels(
    guild: discord.Guild,
    me: discord.Member | None,
) -> list[discord.TextChannel | discord.Thread]:
    channels: list[discord.TextChannel | discord.Thread] = []
    seen_ids: set[int] = set()

    for channel in guild.text_channels:
        channels.append(channel)
        seen_ids.add(channel.id)

    for thread in guild.threads:
        if thread.id not in seen_ids:
            channels.append(thread)
            seen_ids.add(thread.id)

    for text_channel in guild.text_channels:
        if me and not text_channel.permissions_for(me).read_message_history:
            continue
        try:
            async for archived_thread in text_channel.archived_threads(limit=None):
                if archived_thread.id not in seen_ids:
                    channels.append(archived_thread)
                    seen_ids.add(archived_thread.id)
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    return channels


def _resolve_leaderboard_timescale(
    timescale: str,
) -> tuple[str, str, float | None]:
    """(window label, subtitle, cutoff_ts) for a leaderboard window.

    Color is intentionally *not* returned — the leaderboard embed follows the
    guild accent like every other info surface (see embed_style_guide.md); the
    window is conveyed by the title and subtitle, not by a decorative tint.
    """
    now_ts = time.time()
    mapping: dict[str, tuple[str, str, float | None]] = {
        "hour": ("Hourly", "Last 60 minutes", now_ts - 60 * 60),
        "day": ("Daily", "Last 24 hours", now_ts - 24 * 60 * 60),
        "week": ("Weekly", "Last 7 days", now_ts - 7 * 24 * 60 * 60),
        "month": ("Monthly", "Last 30 days", now_ts - 30 * 24 * 60 * 60),
        "year": ("Yearly", "Last 365 days", now_ts - 365 * 24 * 60 * 60),
        "alltime": ("All-Time", "Since tracking began", None),
    }
    return mapping[timescale]


def _format_xp_leaderboard_lines(
    guild: discord.Guild | None,
    entries,
    stats_line: str,
    empty_text: str,
    user_line: str,
) -> str:
    if not entries:
        return f"{stats_line}\n\n{empty_text}\n\n{user_line}"

    rank_icons = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = [stats_line, ""]
    for idx, entry in enumerate(entries, start=1):
        member = guild.get_member(entry.user_id) if guild else None
        label = member.mention if member else f"<@{entry.user_id}>"
        rank = rank_icons[idx - 1] if idx <= len(rank_icons) else f"{idx}."
        lines.append(f"{rank} {label}\n`{entry.xp:.2f} XP`")

    lines.append("")
    lines.append(user_line)
    return "\n".join(lines)


def _format_xp_distribution_summary(
    member_count: int, median_xp: float, stddev_xp: float
) -> str:
    return (
        "**Distribution**\n"
        f"Members: **{member_count}**\n"
        f"Median: `{median_xp:.2f} XP`\n"
        f"Std Dev: `{stddev_xp:.2f} XP`"
    )


def _build_xp_leaderboard_embed(
    ctx: AppContext,
    guild: discord.Guild,
    caller: discord.Member,
    window_name: str,
    subtitle: str,
    color: discord.Color,
    cutoff: float | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏆 {window_name} XP Leaders",
        description=subtitle,
        color=color,
    )

    source_specs = [
        ("Text", "💬", XP_SOURCE_TEXT, "No text XP yet."),
        ("Replies", "↩️", XP_SOURCE_REPLY, "No reply XP yet."),
        ("Voice", "🎙️", XP_SOURCE_VOICE, "No voice XP yet."),
        ("Image Reacts", "🖼️", XP_SOURCE_IMAGE_REACT, "No image react XP yet."),
    ]

    with ctx.open_db() as conn:
        for field_name, icon, source_key, empty_text in source_specs:
            entries = get_xp_leaderboard(
                conn, guild.id, source_key, since_ts=cutoff, limit=5
            )
            distribution = get_xp_distribution_stats(
                conn, guild.id, source_key, since_ts=cutoff
            )
            standing = get_user_xp_standing(
                conn, guild.id, source_key, caller.id, since_ts=cutoff
            )
            stats_line = _format_xp_distribution_summary(
                distribution.member_count,
                distribution.median_xp,
                distribution.stddev_xp,
            )
            if standing.rank is None:
                user_line = f"Your standing: {caller.mention} has no tracked XP here."
            else:
                user_line = f"Your standing: #{standing.rank} {caller.mention} with `{standing.xp:.2f} XP`"
            embed.add_field(
                name=f"{icon} {field_name}",
                value=_format_xp_leaderboard_lines(
                    guild, entries, stats_line, empty_text, user_line
                ),
                inline=True,
            )

    embed.set_footer(text="Top 5 by XP source with your standing")
    return embed


class XpCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        self._xp_rollup_loop.start()

    async def cog_unload(self) -> None:
        self._xp_rollup_loop.cancel()

    # ── Daily xp_events → xp_daily rollup, then retention ────────────

    @tasks.loop(hours=24)
    async def _xp_rollup_loop(self) -> None:
        """Aggregate complete days of xp_events into xp_daily, then prune.

        Stages 1 and 3 of docs/plans/xp-events-retention-and-rollup.md. Order
        is the interlock and is not negotiable: the rollup runs first, and the
        prune only ever deletes days the rollup has already covered — it
        refuses outright if it cannot prove that. A guild that has not opted
        in is skipped, which is every guild until an admin turns retention on.
        """
        try:
            def _roll() -> tuple[int, int, int]:
                with self.bot.ctx.open_db() as conn:
                    days, buckets = xp_rollup_service.rollup_pending_days(
                        conn, limit=_ROLLUP_DAYS_PER_PASS
                    )
                    # Late-arriving events are real (voice XP lands when a
                    # session ends), so the newest complete days are rebuilt
                    # every pass rather than trusted from their first roll.
                    _, refreshed = xp_rollup_service.refresh_recent_days(conn)
                    return days, buckets, refreshed

            days, buckets, refreshed = await asyncio.to_thread(_roll)
            if days or refreshed:
                log.info(
                    "XP rollup: %d new day(s) → %d bucket(s); %d refreshed",
                    days, buckets, refreshed,
                )
        except Exception:
            log.exception("XP daily rollup failed")
            # A failed rollup must not be followed by a prune. The prune has
            # its own guards and would refuse anyway, but not attempting it is
            # the clearer statement.
            return

        try:
            await self._prune_xp_events()
        except Exception:
            log.exception("XP retention prune failed")

    async def _prune_xp_events(self) -> None:
        """Delete raw events the rollup covers, for guilds that opted in."""
        def _prune() -> tuple[int, bool]:
            total = 0
            with self.bot.ctx.open_db() as conn:
                for guild in self.bot.guilds:
                    try:
                        total += xp_rollup_service.prune_raw_events(
                            conn, guild.id, limit=_PRUNE_ROWS_PER_PASS
                        )
                    except xp_rollup_service.PruneRefused as exc:
                        # Only worth a line for a guild that actually asked for
                        # retention; every other guild "refuses" every pass.
                        if xp_rollup_service.retention_enabled(conn, guild.id):
                            log.warning(
                                "XP retention skipped for %s: %s", guild.id, exc
                            )
            return total, total > 0

        deleted, any_deleted = await asyncio.to_thread(_prune)
        if not any_deleted:
            return
        log.info("XP retention: pruned %d raw event(s)", deleted)

        # Deleting rows returns pages to SQLite's freelist; it does not shrink
        # the file, and in WAL mode the deletions sit in the -wal until a
        # checkpoint moves them. The first prune is ~500k rows, so force the
        # checkpoint rather than waiting for the automatic one — see the
        # erasure runbook's note on the same trap.
        def _checkpoint() -> None:
            with self.bot.ctx.open_db() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        try:
            await asyncio.to_thread(_checkpoint)
        except Exception:
            log.exception("WAL checkpoint after the XP prune failed")

    @_xp_rollup_loop.before_loop
    async def _before_xp_rollup(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="xp_leaderboards",
        description="Top XP earners by source (text, voice, replies, images) and your rank.",
    )
    @app_commands.describe(
        timescale="Time window — hour, day, week, month, year, or alltime."
    )
    async def xp_leaderboards(
        self,
        interaction: discord.Interaction,
        timescale: Literal[
            "hour", "day", "week", "month", "year", "alltime"
        ] = "alltime",
    ) -> None:
        ctx = self.bot.ctx
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True
            )
            return

        caller = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if caller is None:
            await interaction.response.send_message(
                "❌ Could not resolve your member record in this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        window_name, subtitle, cutoff = _resolve_leaderboard_timescale(timescale)
        accent = await safe_resolve_accent(ctx, guild, log_label="xp", default=DEFAULT_ACCENT_COLOR)

        def _check_xp():
            with ctx.open_db() as conn:
                has_events = has_any_xp_events(conn, guild.id)
                has_xp = has_any_member_xp(conn, guild.id) if not has_events else False
                return has_events, has_xp

        has_events, has_xp = await asyncio.to_thread(_check_xp)

        if not has_events:
            description = (
                "Existing XP totals predate the event ledger. "
                "New text and voice XP will appear here going forward."
                if has_xp
                else "No XP recorded yet."
            )
            embed = discord.Embed(
                title="🏆 XP Leaderboards",
                description=description,
                color=accent,
            )
            embed.add_field(name="💬 Text", value="No tracked text XP yet.", inline=True)
            embed.add_field(
                name="↩️ Replies", value="No tracked reply XP yet.", inline=True
            )
            embed.add_field(
                name="🎙️ Voice", value="No tracked voice XP yet.", inline=True
            )
            embed.add_field(
                name="🖼️ Image Reacts",
                value="No tracked image react XP yet.",
                inline=True,
            )
            embed.set_footer(text="Top 5 by XP source and time window")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = await asyncio.to_thread(
            _build_xp_leaderboard_embed,
            ctx,
            guild,
            caller,
            window_name,
            subtitle,
            accent,
            cutoff,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="xp_give", description="Award 20 XP to a member.")
    @app_commands.describe(member="Who to give the XP to.")
    async def xp_give(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        ctx = self.bot.ctx
        if not ctx.can_use_xp_grant(interaction):
            await interaction.response.send_message(
                NO_PERMISSION, ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command only works in a server.", ephemeral=True
            )
            return
        cfg = ctx.guild_config(guild.id)

        if member.bot:
            await interaction.response.send_message(
                "❌ Bots cannot receive XP grants.", ephemeral=True
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You can't grant XP to yourself.", ephemeral=True
            )
            return

        now_ts = time.time()
        guild_id = guild.id
        member_id = member.id
        xp_settings = cfg.xp_settings

        def _do_xp():
            with ctx.open_db() as conn:
                return apply_xp_award(
                    conn,
                    guild_id,
                    member_id,
                    xp_settings.manual_grant_xp,
                    event_source=XP_SOURCE_GRANT,
                    event_timestamp=now_ts,
                    settings=xp_settings,
                )

        award = await asyncio.to_thread(_do_xp)

        await handle_level_progress(
            member,
            award,
            "manual_grant",
            level_5_role_id=cfg.level_5_role_id,
            level_up_log_channel_id=cfg.level_up_log_channel_id,
            level_5_log_channel_id=cfg.level_5_log_channel_id,
            settings=xp_settings,
            db_path=ctx.db_path,
            nsfw_role_id=nsfw_grant_role_id(cfg.grant_roles, cfg.promotion_review_grant_role_id),
        )

        await interaction.response.send_message(
            f"{interaction.user.mention} granted {cfg.xp_settings.manual_grant_xp:.0f} XP to {member.mention}. "
            f"They now have {award.total_xp:.2f} XP and are level {award.new_level}.",
            ephemeral=False,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(XpCog(bot))
