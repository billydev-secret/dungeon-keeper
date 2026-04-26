"""XP commands."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from services.embeds import XP_PRIMARY
from services.xp_service import handle_level_progress
from xp_system import (
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

if TYPE_CHECKING:
    from app_context import AppContext, Bot


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
) -> tuple[str, str, discord.Color, float | None]:
    now_ts = time.time()
    mapping = {
        "hour": (
            "Hourly",
            "Last 60 minutes",
            discord.Color.dark_teal(),
            now_ts - 60 * 60,
        ),
        "day": ("Daily", "Last 24 hours", discord.Color.blue(), now_ts - 24 * 60 * 60),
        "week": (
            "Weekly",
            "Last 7 days",
            discord.Color.teal(),
            now_ts - 7 * 24 * 60 * 60,
        ),
        "month": (
            "Monthly",
            "Last 30 days",
            discord.Color.orange(),
            now_ts - 30 * 24 * 60 * 60,
        ),
        "year": (
            "Yearly",
            "Last 365 days",
            discord.Color.brand_green(),
            now_ts - 365 * 24 * 60 * 60,
        ),
        "alltime": ("All-Time", "Since tracking began", discord.Color.gold(), None),
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
        title=f"{window_name} XP Leaders",
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
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

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
        ctx = self.ctx
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        caller = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if caller is None:
            await interaction.response.send_message(
                "Could not resolve your member record in this guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        window_name, subtitle, color, cutoff = _resolve_leaderboard_timescale(timescale)

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
                title="XP Leaderboards",
                description=description,
                color=XP_PRIMARY,
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
            color,
            cutoff,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="xp_give", description="Award 20 XP to a member.")
    @app_commands.describe(member="Who to give the XP to.")
    async def xp_give(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        ctx = self.ctx
        if not ctx.can_use_xp_grant(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "Bots cannot receive XP grants.", ephemeral=True
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't grant XP to yourself.", ephemeral=True
            )
            return

        now_ts = time.time()
        with ctx.open_db() as conn:
            award = apply_xp_award(
                conn,
                guild.id,
                member.id,
                ctx.xp_settings.manual_grant_xp,
                event_source=XP_SOURCE_GRANT,
                event_timestamp=now_ts,
                settings=ctx.xp_settings,
            )

        await handle_level_progress(
            member,
            award,
            "manual_grant",
            level_5_role_id=ctx.level_5_role_id,
            level_up_log_channel_id=ctx.level_up_log_channel_id,
            level_5_log_channel_id=ctx.level_5_log_channel_id,
        )

        await interaction.response.send_message(
            f"{interaction.user.mention} granted {ctx.xp_settings.manual_grant_xp:.0f} XP to {member.mention}. "
            f"They now have {award.total_xp:.2f} XP and are level {award.new_level}.",
            ephemeral=False,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(XpCog(bot, bot.ctx))
