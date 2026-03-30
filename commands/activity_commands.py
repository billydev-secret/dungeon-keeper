"""Activity graph slash commands.

Commands:
  /activity        — bar chart of message volume (server-wide or per member/channel)
  /dropoff         — members with the largest recent message-rate decline
  /session_burst   — per-member session burst profile (activity after a 20-min absence)
  /burst_ranking   — server-wide ranking of highest/lowest session burst increase
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, cast

import discord
from discord import app_commands

from services.activity_graphs import (
    _HOD_LABELS,
    _WINDOW_LABELS,
    DropoffProfile,
    query_burst_ranking,
    query_dropoff_profiles,
    query_message_activity,
    query_message_histogram,
    query_session_burst,
    render_activity_chart,
    render_burst_ranking_chart,
    render_session_burst_chart,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_activity_commands(bot: "Bot", ctx: "AppContext") -> None:
    @bot.tree.command(
        name="activity",
        description="Show a message activity chart for the server or a specific member.",
    )
    @app_commands.describe(
        resolution="Time resolution for the chart buckets.",
        member="Show activity for this member only (default: whole server).",
        channel="Filter activity to a specific channel.",
    )
    async def activity(
        interaction: discord.Interaction,
        resolution: Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"] = "day",
        member: discord.Member | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        window_label = _WINDOW_LABELS[resolution]

        if member is not None and channel is not None:
            title = f"{member.display_name} in #{channel.name} — Activity ({window_label})"
        elif member is not None:
            title = f"{member.display_name} — Activity ({window_label})"
        elif channel is not None:
            title = f"#{channel.name} — Activity ({window_label})"
        else:
            title = f"{guild.name} — Activity ({window_label})"

        user_id = member.id if member is not None else None
        channel_id = channel.id if channel is not None else None
        with ctx.open_db() as conn:
            if resolution in ("hour_of_day", "day_of_week"):
                labels, msg_counts = query_message_histogram(
                    conn, guild.id, cast(Literal["hour_of_day", "day_of_week"], resolution),
                    user_id=user_id, channel_id=channel_id,
                )
                member_counts: list[int] = []
                show_members = False
            else:
                labels, msg_counts, member_counts = query_message_activity(
                    conn, guild.id, resolution, user_id=user_id, channel_id=channel_id,
                )
                show_members = member is None and channel is None

        if not any(c > 0 for c in msg_counts):
            await interaction.followup.send(
                f"No message activity recorded for the {window_label.lower()}.",
                ephemeral=True,
            )
            return

        chart_bytes = render_activity_chart(
            labels,
            msg_counts,
            member_counts,
            title=title,
            resolution=resolution,
            show_members=show_members,
        )

        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="activity.png"),
            ephemeral=True,
        )

    _DROPOFF_PERIOD_SECONDS: dict[str, float] = {
        "day": 24 * 60 * 60,
        "week": 7 * 24 * 60 * 60,
        "month": 30 * 24 * 60 * 60,
    }

    # ── dropoff formatting helpers ────────────────────────────────────────

    def _pct(prev: int | float, recent: int | float) -> str:
        if not prev:
            return ""
        p = round((recent - prev) / prev * 100)
        return f"{p:+d}%"

    def _arrow(prev: int | float, recent: int | float, fmt: str = "g") -> str:
        return f"{prev:{fmt}} \u2192 {recent:{fmt}}"

    def _last_seen_str(ts: float | None) -> str:
        if ts is None:
            return "unknown"
        delta = datetime.now(timezone.utc).timestamp() - ts
        if delta < 3600:
            return f"{max(1, int(delta / 60))}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{delta / 86400:.1f}d ago"

    def _gap_str(secs: float) -> str:
        if secs < 3600:
            return f"{max(1, int(secs / 60))}m"
        if secs < 86400:
            return f"{secs / 3600:.1f}h"
        return f"{secs / 86400:.1f}d"

    def _vs_server(p: DropoffProfile) -> str:
        """User's msg drop relative to the server-wide trend."""
        srv_pct = ((p.server_msgs_recent - p.server_msgs_prev) / p.server_msgs_prev * 100
                   if p.server_msgs_prev else 0.0)
        usr_pct = ((p.msgs_recent - p.msgs_prev) / p.msgs_prev * 100
                   if p.msgs_prev else 0.0)
        diff = round(usr_pct - srv_pct)
        return f"{diff:+d}pp vs server"

    def _server_header(p: DropoffProfile) -> str:
        """One-line server-wide trend shown above the ranked list."""
        return (
            f"Server trend: **{_arrow(p.server_msgs_prev, p.server_msgs_recent)}** msgs"
            f" ({_pct(p.server_msgs_prev, p.server_msgs_recent)})\n"
        )

    def _fmt_compact(
        rank: int, p: DropoffProfile, guild: discord.Guild,
    ) -> str:
        """Format one user for the ranked list embed."""
        member = guild.get_member(p.user_id)
        name = member.mention if member else f"<@{p.user_id}>"
        lvl = f" (Lv {p.level})" if p.level else ""

        msg_drop = _pct(p.msgs_prev, p.msgs_recent)
        voice = ""
        if p.voice_xp_prev or p.voice_xp_recent:
            voice = f" \u00b7 Voice {_pct(p.voice_xp_prev, p.voice_xp_recent) or 'n/a'}"

        parts = [
            f"`{rank:>2}.` {name}{lvl}",
            (
                f"    Msgs `{_arrow(p.msgs_prev, p.msgs_recent)}` ({msg_drop}, {_vs_server(p)})"
                f"{voice}"
                f" \u00b7 Days `{p.days_prev}\u2192{p.days_recent}`/{p.days_in_window}"
            ),
            (
                f"    Channels `{p.channels_prev}\u2192{p.channels_recent}`"
                f" \u00b7 Partners `{p.partners_prev}\u2192{p.partners_recent}`"
                f" \u00b7 Last seen {_last_seen_str(p.last_seen_ts)}"
            ),
        ]
        return "\n".join(parts)

    def _ch_name(guild: discord.Guild, cid: int) -> str:
        ch = guild.get_channel(cid)
        return f"#{ch.name}" if ch and hasattr(ch, "name") else f"#{cid}"

    def _fmt_detail(
        p: DropoffProfile, guild: discord.Guild, period_label: str,
    ) -> discord.Embed:
        """Build a full-detail embed for a single user."""
        member = guild.get_member(p.user_id)
        name = member.display_name if member else f"User {p.user_id}"

        srv_trend = _pct(p.server_msgs_prev, p.server_msgs_recent)
        lvl_note = f"  \u00b7  Level {p.level} ({p.total_xp:,.0f} XP)" if p.level else ""
        embed = discord.Embed(
            title=f"Engagement Profile \u2014 {name}",
            description=(
                f"Comparing the prior {period_label} to the most recent {period_label}.{lvl_note}\n"
                f"Server trend: **{_arrow(p.server_msgs_prev, p.server_msgs_recent)}** msgs ({srv_trend})"
            ),
            color=discord.Color.orange(),
        )
        if member and member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        # Activity
        extras = ""
        if p.longest_gap_secs > 0:
            extras += f"\nLongest silence: {_gap_str(p.longest_gap_secs)}"
        if p.first_activity_day is not None and p.first_activity_day > 0:
            extras += f"\nFirst active: day {p.first_activity_day} of {p.days_in_window}"
        embed.add_field(name="Activity", value=(
            f"Messages: **{_arrow(p.msgs_prev, p.msgs_recent)}** ({_pct(p.msgs_prev, p.msgs_recent)}, {_vs_server(p)})\n"
            f"Days active: **{p.days_prev}/{p.days_in_window}** \u2192 **{p.days_recent}/{p.days_in_window}**\n"
            f"Channels: **{_arrow(p.channels_prev, p.channels_recent)}**\n"
            f"Last seen: {_last_seen_str(p.last_seen_ts)}"
            f"{extras}"
        ), inline=False)

        # XP Breakdown
        xp_lines = []
        if p.text_xp_prev or p.text_xp_recent:
            xp_lines.append(
                f"Text: **{_arrow(p.text_xp_prev, p.text_xp_recent, '.0f')}** ({_pct(p.text_xp_prev, p.text_xp_recent)})"
            )
        if p.reply_xp_prev or p.reply_xp_recent:
            xp_lines.append(
                f"Reply: **{_arrow(p.reply_xp_prev, p.reply_xp_recent, '.0f')}** ({_pct(p.reply_xp_prev, p.reply_xp_recent)})"
            )
        if p.voice_xp_prev or p.voice_xp_recent:
            xp_lines.append(
                f"Voice: **{_arrow(p.voice_xp_prev, p.voice_xp_recent, '.0f')}** ({_pct(p.voice_xp_prev, p.voice_xp_recent)})"
            )
        if p.image_react_xp_prev or p.image_react_xp_recent:
            xp_lines.append(
                f"Image react: **{_arrow(p.image_react_xp_prev, p.image_react_xp_recent, '.0f')}** ({_pct(p.image_react_xp_prev, p.image_react_xp_recent)})"
            )
        if xp_lines:
            embed.add_field(name="XP Breakdown", value="\n".join(xp_lines), inline=False)

        # Conversations
        reply_pct_prev = round(p.replies_prev / p.msgs_prev * 100) if p.msgs_prev else 0
        reply_pct_recent = round(p.replies_recent / p.msgs_recent * 100) if p.msgs_recent else 0
        convo_val = (
            f"Replies: **{_arrow(p.replies_prev, p.replies_recent)}** ({reply_pct_prev}% \u2192 {reply_pct_recent}%)\n"
            f"Initiations: **{_arrow(p.initiations_prev, p.initiations_recent)}** ({_pct(p.initiations_prev, p.initiations_recent)})\n"
            f"Avg length: **{_arrow(round(p.avg_len_prev), round(p.avg_len_recent))}** chars"
        )
        if p.deep_convos_prev or p.deep_convos_recent:
            convo_val += (
                f"\nDeep threads (3+): **{_arrow(p.deep_convos_prev, p.deep_convos_recent)}**"
            )
        embed.add_field(name="Conversations", value=convo_val, inline=False)

        # Social
        embed.add_field(name="Social", value=(
            f"Unique partners: **{_arrow(p.partners_prev, p.partners_recent)}**\n"
            f"Inbound mentions: **{_arrow(p.inbound_prev, p.inbound_recent)}** ({_pct(p.inbound_prev, p.inbound_recent)})\n"
            f"Outbound mentions: **{_arrow(p.outbound_prev, p.outbound_recent)}** ({_pct(p.outbound_prev, p.outbound_recent)})"
        ), inline=False)

        # Content
        if p.attachments_prev or p.attachments_recent or p.reactions_prev or p.reactions_recent:
            content_lines = []
            if p.attachments_prev or p.attachments_recent:
                content_lines.append(
                    f"Attachments: **{_arrow(p.attachments_prev, p.attachments_recent)}**"
                )
            if p.reactions_prev or p.reactions_recent:
                rpm_prev = p.reactions_prev / p.msgs_prev if p.msgs_prev else 0
                rpm_recent = p.reactions_recent / p.msgs_recent if p.msgs_recent else 0
                content_lines.append(
                    f"Reactions received: **{_arrow(p.reactions_prev, p.reactions_recent)}**"
                    f" ({rpm_prev:.1f} \u2192 {rpm_recent:.1f} per msg)"
                )
            embed.add_field(name="Content", value="\n".join(content_lines), inline=False)

        # Channel Migration
        if p.channels_left or p.channels_joined:
            migration_lines = []
            if p.channels_left:
                left_names = ", ".join(_ch_name(guild, c) for c in p.channels_left[:8])
                migration_lines.append(f"Left: {left_names}")
            if p.channels_joined:
                joined_names = ", ".join(_ch_name(guild, c) for c in p.channels_joined[:8])
                migration_lines.append(f"Joined: {joined_names}")
            if p.channels_stayed:
                stayed_names = ", ".join(_ch_name(guild, c) for c in p.channels_stayed[:8])
                migration_lines.append(f"Still active: {stayed_names}")
            embed.add_field(name="Channel Migration", value="\n".join(migration_lines), inline=False)

        # Patterns
        pattern_lines = []
        if p.peak_hour_prev is not None or p.peak_hour_recent is not None:
            h_prev = _HOD_LABELS[p.peak_hour_prev] if p.peak_hour_prev is not None else "\u2014"
            h_recent = _HOD_LABELS[p.peak_hour_recent] if p.peak_hour_recent is not None else "\u2014"
            pattern_lines.append(f"Peak hour: **{h_prev} \u2192 {h_recent}**")
        if p.msgs_prev or p.msgs_recent:
            pattern_lines.append(
                f"Weekday msgs: **{p.weekday_pct_prev:.0f}% \u2192 {p.weekday_pct_recent:.0f}%**"
            )
        if pattern_lines:
            embed.add_field(name="Patterns", value="\n".join(pattern_lines), inline=False)

        return embed

    # ── command ───────────────────────────────────────────────────────────

    @bot.tree.command(
        name="dropoff",
        description="Show members with the largest drop in engagement between two equal time windows.",
    )
    @app_commands.describe(
        period="Length of each comparison window.",
        limit="Number of members to show (1\u201325, default 10).",
        channel="Restrict candidate selection to a specific channel.",
        member="Show a detailed engagement profile for one member instead of the ranked list.",
    )
    async def dropoff(
        interaction: discord.Interaction,
        period: Literal["day", "week", "month"] = "week",
        limit: app_commands.Range[int, 1, 25] = 10,
        channel: discord.TextChannel | None = None,
        member: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        period_secs = _DROPOFF_PERIOD_SECONDS[period]
        period_label = period
        channel_id = channel.id if channel is not None else None

        with ctx.open_db() as conn:
            profiles = query_dropoff_profiles(
                conn, guild.id, period_secs,
                channel_id=channel_id, limit=limit,
                target_user_id=member.id if member else None,
            )

        # ── detail view (single member) ───────────────────────────────────
        if member is not None:
            if not profiles:
                await interaction.followup.send(
                    f"No message data found for {member.mention} in the last two {period_label}s.",
                    ephemeral=True,
                )
                return
            embed = _fmt_detail(profiles[0], guild, period_label)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # ── ranked list view ──────────────────────────────────────────────
        if not profiles:
            suffix = f" in #{channel.name}" if channel else ""
            await interaction.followup.send(
                f"No significant engagement drops found{suffix} "
                f"comparing the last {period_label} to the prior {period_label}.",
                ephemeral=True,
            )
            return

        header = _server_header(profiles[0])
        lines = [
            _fmt_compact(rank, p, guild)
            for rank, p in enumerate(profiles, start=1)
        ]

        if channel:
            title = f"Engagement Dropoff in #{channel.name}"
        else:
            title = f"Engagement Dropoff \u2014 {guild.name}"

        embed = discord.Embed(
            title=title,
            description=header + "\n".join(lines),
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"Prior {period_label} vs most recent {period_label}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="session_burst",
        description="Show a member's message activity profile after returning from a 20-min absence.",
    )
    @app_commands.describe(member="The member to profile.")
    async def session_burst(
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            pre_sessions, post_sessions, overall_rate = query_session_burst(
                conn, guild.id, member.id
            )

        if not post_sessions:
            await interaction.followup.send(
                f"Not enough message history recorded for {member.mention} "
                "to build a session profile.",
                ephemeral=True,
            )
            return

        chart_bytes = render_session_burst_chart(
            pre_sessions,
            post_sessions,
            overall_rate,
            user_display_name=member.display_name,
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="session_burst.png"),
            ephemeral=True,
        )

    @bot.tree.command(
        name="burst_ranking",
        description="Show which members have the highest and lowest session burst increase server-wide.",
    )
    @app_commands.describe(
        limit="Number of members to show at each end (1–15, default 5).",
    )
    async def burst_ranking(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 15] = 5,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            ranking = query_burst_ranking(conn, guild.id)

        if not ranking:
            await interaction.followup.send(
                "Not enough session data to build a burst ranking. "
                "Members need at least 3 sessions (20-min gaps between messages) recorded.",
                ephemeral=True,
            )
            return

        # Resolve display names
        entries: list[tuple[str, float, float, int]] = []
        for user_id, pre_avg, post_avg, n_sessions in ranking:
            member = guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            entries.append((name, pre_avg, post_avg, n_sessions))

        chart_bytes = render_burst_ranking_chart(entries, limit=limit, guild_name=guild.name)
        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="burst_ranking.png"),
            ephemeral=True,
        )
