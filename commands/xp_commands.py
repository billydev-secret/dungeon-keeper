"""XP-related slash commands."""
from __future__ import annotations

import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands

from services.xp_service import handle_level_progress, maybe_grant_level_role
from utils import get_bot_member
from xp_system import (
    DEFAULT_XP_SETTINGS,
    XP_SOURCE_GRANT,
    XP_SOURCE_IMAGE_REACT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    MessageXpContext,
    apply_xp_award,
    calculate_message_xp,
    get_time_to_level_seconds,
    get_user_xp_standing,
    get_xp_distribution_stats,
    get_xp_leaderboard,
    has_any_member_xp,
    has_any_xp_events,
    is_channel_xp_eligible,
    is_message_processed,
    mark_message_processed,
    normalize_message_content,
    record_member_activity,
    record_xp_event,
    update_pair_state,
    xp_required_for_level,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot
    from xp_system import PairState


async def _collect_backfill_channels(
    guild: discord.Guild,
    me: discord.Member | None,
) -> list[discord.TextChannel | discord.Thread]:
    """Return all text channels plus active and archived threads the bot can read."""
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


def _resolve_leaderboard_timescale(timescale: str) -> tuple[str, str, discord.Color, float | None]:
    now_ts = time.time()
    mapping = {
        "hour": ("Hourly", "Last 60 minutes", discord.Color.dark_teal(), now_ts - 60 * 60),
        "day": ("Daily", "Last 24 hours", discord.Color.blue(), now_ts - 24 * 60 * 60),
        "week": ("Weekly", "Last 7 days", discord.Color.teal(), now_ts - 7 * 24 * 60 * 60),
        "month": ("Monthly", "Last 30 days", discord.Color.orange(), now_ts - 30 * 24 * 60 * 60),
        "year": ("Yearly", "Last 365 days", discord.Color.brand_green(), now_ts - 365 * 24 * 60 * 60),
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


def _format_xp_distribution_summary(member_count: int, median_xp: float, stddev_xp: float) -> str:
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
            entries = get_xp_leaderboard(conn, guild.id, source_key, since_ts=cutoff, limit=5)
            distribution = get_xp_distribution_stats(conn, guild.id, source_key, since_ts=cutoff)
            standing = get_user_xp_standing(conn, guild.id, source_key, caller.id, since_ts=cutoff)
            stats_line = _format_xp_distribution_summary(
                distribution.member_count, distribution.median_xp, distribution.stddev_xp
            )
            if standing.rank is None:
                user_line = f"Your standing: {caller.mention} has no tracked XP here."
            else:
                user_line = f"Your standing: #{standing.rank} {caller.mention} with `{standing.xp:.2f} XP`"
            embed.add_field(
                name=f"{icon} {field_name}",
                value=_format_xp_leaderboard_lines(guild, entries, stats_line, empty_text, user_line),
                inline=True,
            )

    embed.set_footer(text="Top 5 by XP source with your standing")
    return embed


def register_xp_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="xp_leaderboards",
        description="Show top 5 XP earners for a selected timescale, plus your standing.",
    )
    @app_commands.describe(timescale="Choose the leaderboard window.")
    async def xp_leaderboards(
        interaction: discord.Interaction,
        timescale: Literal["hour", "day", "week", "month", "year", "alltime"] = "alltime",
    ):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        caller = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        window_name, subtitle, color, cutoff = _resolve_leaderboard_timescale(timescale)

        with ctx.open_db() as conn:
            if not has_any_xp_events(conn, guild.id):
                description = (
                    "Existing XP totals predate the event ledger. "
                    "New text and voice XP will appear here going forward."
                    if has_any_member_xp(conn, guild.id)
                    else "No XP recorded yet."
                )
                embed = discord.Embed(
                    title="XP Leaderboards",
                    description=description,
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="💬 Text", value="No tracked text XP yet.", inline=True)
                embed.add_field(name="↩️ Replies", value="No tracked reply XP yet.", inline=True)
                embed.add_field(name="🎙️ Voice", value="No tracked voice XP yet.", inline=True)
                embed.add_field(name="🖼️ Image Reacts", value="No tracked image react XP yet.", inline=True)
                embed.set_footer(text="Top 5 by XP source and time window")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if caller is None:
            await interaction.response.send_message(
                "Could not resolve your member record in this guild.", ephemeral=True
            )
            return

        embed = _build_xp_leaderboard_embed(ctx, guild, caller, window_name, subtitle, color, cutoff)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="xp_give", description="Give a member 20 XP.")
    @app_commands.describe(member="Member to receive the XP.")
    async def xp_give(interaction: discord.Interaction, member: discord.Member):
        if not ctx.can_use_xp_grant(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        if member.bot:
            await interaction.response.send_message("Bots cannot receive XP grants.", ephemeral=True)
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message("You can't grant XP to yourself.", ephemeral=True)
            return

        now_ts = time.time()
        with ctx.open_db() as conn:
            award = apply_xp_award(
                conn,
                guild.id,
                member.id,
                DEFAULT_XP_SETTINGS.manual_grant_xp,
                event_source=XP_SOURCE_GRANT,
                event_timestamp=now_ts,
                settings=DEFAULT_XP_SETTINGS,
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
            f"{interaction.user.mention} granted {DEFAULT_XP_SETTINGS.manual_grant_xp:.0f} XP to {member.mention}. "
            f"They now have {award.total_xp:.2f} XP and are level {award.new_level}.",
            ephemeral=False,
        )

    @bot.tree.command(name="xp_give_allow", description="Allow a user to use /xp_give.")
    @app_commands.describe(member="User to add to the /xp_give allowlist.")
    async def xp_give_allow(interaction: discord.Interaction, member: discord.Member):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        ctx.xp_grant_allowed_user_ids = ctx.add_config_id_value("xp_grant_allowed_user_ids", member.id)
        await interaction.response.send_message(
            f"{member.mention} can now use /xp_give. "
            f"Allowed user IDs: {sorted(ctx.xp_grant_allowed_user_ids)}",
            ephemeral=True,
        )

    @bot.tree.command(name="xp_give_disallow", description="Remove a user from /xp_give access.")
    @app_commands.describe(member="User to remove from the /xp_give allowlist.")
    async def xp_give_disallow(interaction: discord.Interaction, member: discord.Member):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        ctx.xp_grant_allowed_user_ids = ctx.remove_config_id_value("xp_grant_allowed_user_ids", member.id)
        await interaction.response.send_message(
            f"{member.mention} can no longer use /xp_give. "
            f"Allowed user IDs: {sorted(ctx.xp_grant_allowed_user_ids)}",
            ephemeral=True,
        )

    @bot.tree.command(name="xp_give_allowed", description="List users allowed to use /xp_give.")
    async def xp_give_allowed(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ctx.xp_grant_allowed_user_ids:
            await interaction.response.send_message(
                "No regular users are currently allowed to use /xp_give.", ephemeral=True
            )
            return

        guild = interaction.guild
        labels = []
        for user_id in sorted(ctx.xp_grant_allowed_user_ids):
            m = guild.get_member(user_id) if guild else None
            labels.append(m.mention if m else f"`{user_id}`")

        await interaction.response.send_message(
            "Users allowed to use /xp_give: " + ", ".join(labels), ephemeral=True
        )

    @bot.tree.command(
        name="xp_set_levelup_log_here",
        description="Send level-up announcements to this channel or thread.",
    )
    async def xp_set_levelup_log_here(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        ctx.level_up_log_channel_id = int(ctx.set_config_value("xp_level_up_log_channel_id", str(channel.id)))
        await interaction.response.send_message(
            f"Level-up announcements will be posted in {channel.mention}.", ephemeral=True
        )

    @bot.tree.command(
        name="xp_set_level5_log_here",
        description="Send level 5 XP announcements to this channel or thread.",
    )
    async def xp_set_level5_log_here(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        ctx.level_5_log_channel_id = int(ctx.set_config_value("xp_level_5_log_channel_id", str(channel.id)))
        await interaction.response.send_message(
            f"Level {DEFAULT_XP_SETTINGS.role_grant_level} announcements will be posted in {channel.mention}.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="xp_exclude_here",
        description="Disable XP gain in this channel or thread.",
    )
    async def xp_exclude_here(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        ctx.xp_excluded_channel_ids = ctx.add_config_id_value("xp_excluded_channel_ids", channel.id)
        await interaction.response.send_message(
            f"XP excluded for {channel.mention}. "
            f"Excluded channel IDs: {sorted(ctx.xp_excluded_channel_ids)}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="xp_include_here",
        description="Re-enable XP gain in this channel or thread.",
    )
    async def xp_include_here(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        ctx.xp_excluded_channel_ids = ctx.remove_config_id_value("xp_excluded_channel_ids", channel.id)
        await interaction.response.send_message(
            f"XP enabled for {channel.mention}. "
            f"Excluded channel IDs: {sorted(ctx.xp_excluded_channel_ids)}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="xp_excluded_channels",
        description="List channels and threads where XP is currently disabled.",
    )
    async def xp_excluded_channels(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ctx.xp_excluded_channel_ids:
            await interaction.response.send_message("XP is currently enabled in all channels.", ephemeral=True)
            return

        guild = interaction.guild
        labels = []
        for channel_id in sorted(ctx.xp_excluded_channel_ids):
            channel = guild.get_channel(channel_id) if guild else None
            labels.append(channel.mention if channel else f"`{channel_id}`")

        await interaction.response.send_message("XP excluded in: " + ", ".join(labels), ephemeral=True)

    @bot.tree.command(
        name="xp_backfill_history",
        description="Scan message history to fill gaps in XP and activity tracking.",
    )
    @app_commands.describe(days="How many days back to scan. Use 0 for all available history.")
    async def xp_backfill_history(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 3650] = 0,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        now_dt = datetime.now(timezone.utc)
        after_dt = None if days == 0 else now_dt - timedelta(days=days)
        granted_members: dict[int, discord.Member] = {}
        backfill_user_state: dict[int, tuple[float, str]] = {}
        pair_states: dict[int, PairState] = {}
        stats = {
            "channels_scanned": 0,
            "messages_seen": 0,
            "messages_processed": 0,
            "messages_skipped_processed": 0,
            "messages_awarded": 0,
            "xp_awarded": 0.0,
        }

        me = get_bot_member(guild)
        all_channels = await _collect_backfill_channels(guild, me)

        with ctx.open_db() as conn:
            for channel in all_channels:
                channel_id: int | None = getattr(channel, "id", None)
                parent_id = getattr(channel, "parent_id", None)
                if channel_id is None or not is_channel_xp_eligible(channel_id, parent_id, ctx.xp_excluded_channel_ids):
                    continue

                if me and not channel.permissions_for(me).read_message_history:
                    continue

                stats["channels_scanned"] += 1
                channel_pair_state = pair_states.get(channel.id)

                try:
                    async for message in channel.history(
                        limit=None, after=after_dt, oldest_first=True
                    ):
                        stats["messages_seen"] += 1

                        if not message.guild or message.author.bot:
                            continue

                        if is_message_processed(conn, guild.id, message.id):
                            stats["messages_skipped_processed"] += 1
                            continue

                        # Use the pre-resolved reference if available; never fetch
                        # during backfill to avoid per-message API calls at scale.
                        resolved_ref = (
                            message.reference.resolved
                            if message.reference and isinstance(message.reference.resolved, discord.Message)
                            else None
                        )
                        is_reply_to_human = bool(
                            resolved_ref
                            and not resolved_ref.author.bot
                            and resolved_ref.author.id != message.author.id
                        )

                        now_ts = message.created_at.timestamp() if message.created_at else time.time()
                        normalized_content = normalize_message_content(message.content)
                        channel_pair_state, pair_streak = update_pair_state(
                            channel_pair_state, message.author.id
                        )
                        pair_states[channel.id] = channel_pair_state

                        prior_ts = None
                        prior_norm = None
                        if message.author.id in backfill_user_state:
                            prior_ts, prior_norm = backfill_user_state[message.author.id]

                        breakdown = calculate_message_xp(
                            MessageXpContext(
                                content=message.content,
                                seconds_since_last_message=(
                                    None if prior_ts is None else now_ts - prior_ts
                                ),
                                is_duplicate=bool(normalized_content) and normalized_content == prior_norm,
                                is_reply_to_human=is_reply_to_human,
                                pair_streak=pair_streak,
                            ),
                            DEFAULT_XP_SETTINGS,
                        )

                        award = apply_xp_award(
                            conn,
                            guild.id,
                            message.author.id,
                            breakdown.awarded_xp,
                            settings=DEFAULT_XP_SETTINGS,
                        )

                        reply_award = 0.0
                        if breakdown.reply_bonus_xp > 0:
                            reply_award = round(
                                breakdown.reply_bonus_xp
                                * breakdown.cooldown_multiplier
                                * breakdown.duplicate_multiplier
                                * breakdown.pair_multiplier,
                                2,
                            )
                        text_award = round(max(0.0, award.awarded_xp - reply_award), 2)
                        record_xp_event(conn, guild.id, message.author.id, XP_SOURCE_TEXT, text_award, now_ts)
                        record_xp_event(conn, guild.id, message.author.id, XP_SOURCE_REPLY, reply_award, now_ts)
                        mark_message_processed(
                            conn,
                            guild.id,
                            message.id,
                            message.channel.id,
                            message.author.id,
                            now_ts,
                        )
                        record_member_activity(
                            conn,
                            guild.id,
                            message.author.id,
                            message.channel.id,
                            message.id,
                            now_ts,
                        )

                        backfill_user_state[message.author.id] = (now_ts, normalized_content)
                        stats["messages_processed"] += 1
                        if award.awarded_xp > 0:
                            stats["messages_awarded"] += 1
                            stats["xp_awarded"] += award.awarded_xp
                            m = (
                                message.author
                                if isinstance(message.author, discord.Member)
                                else guild.get_member(message.author.id)
                            )
                            if m and award.new_level >= DEFAULT_XP_SETTINGS.role_grant_level:
                                granted_members[m.id] = m
                except discord.Forbidden:
                    continue

        for m in granted_members.values():
            await maybe_grant_level_role(m, DEFAULT_XP_SETTINGS.role_grant_level, ctx.level_5_role_id)

        window_label = "all available history" if days == 0 else f"last {days} days"
        await interaction.followup.send(
            (
                f"Backfill complete for {window_label}.\n"
                f"Channels scanned: {stats['channels_scanned']}\n"
                f"Messages seen: {stats['messages_seen']}\n"
                f"Messages processed: {stats['messages_processed']}\n"
                f"Already processed: {stats['messages_skipped_processed']}\n"
                f"Messages awarding XP: {stats['messages_awarded']}\n"
                f"XP added: {stats['xp_awarded']:.2f}"
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="xp_level_review",
        description="Show how long it takes members to reach a given level (avg, mode, std dev).",
    )
    @app_commands.describe(level="The level to measure time-to-reach for (minimum 2).")
    async def xp_level_review(
        interaction: discord.Interaction,
        level: app_commands.Range[int, 2, 100],
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        with ctx.open_db() as conn:
            durations = get_time_to_level_seconds(conn, guild.id, level)

        if not durations:
            xp_needed = xp_required_for_level(level)
            await interaction.followup.send(
                f"No members have reached level {level} yet "
                f"({xp_needed:.0f} XP required).",
                ephemeral=True,
            )
            return

        mean_s = statistics.mean(durations)
        stddev_s = statistics.pstdev(durations)

        # Mode: bucket by day, find most common bucket
        day_buckets = Counter(int(s // 86400) for s in durations)
        modal_days, modal_count = day_buckets.most_common(1)[0]

        def fmt(seconds: float) -> str:
            d = int(seconds // 86400)
            h = int((seconds % 86400) // 3600)
            if d > 0:
                return f"{d}d {h}h"
            m = int((seconds % 3600) // 60)
            return f"{h}h {m}m"

        xp_needed = xp_required_for_level(level)
        report = (
            f"**Time to Reach Level {level}** ({xp_needed:.0f} XP required)\n"
            f"Members who reached it: **{len(durations)}**\n"
            f"Average: `{fmt(mean_s)}`\n"
            f"Mode: `{modal_days}d` ({modal_count} member{'s' if modal_count != 1 else ''})\n"
            f"Std Dev: `{fmt(stddev_s)}`"
        )
        await interaction.followup.send(report, ephemeral=True)
