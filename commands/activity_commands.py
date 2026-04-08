"""Activity graph slash commands.

Commands:
  /activity        — bar chart of message volume (server-wide or per member/channel)
  /dropoff         — members with the largest recent message-rate decline
  /session_burst   — per-member session burst profile (activity after a 20-min absence)
  /burst_ranking   — server-wide ranking of highest/lowest session burst increase
"""
from __future__ import annotations

import asyncio
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
    query_xp_activity,
    query_xp_histogram,
    render_activity_chart,
    render_burst_ranking_chart,
    render_session_burst_chart,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_activity_commands(bot: "Bot", ctx: "AppContext") -> None:
    @bot.tree.command(
        name="activity",
        description="Show a message or XP activity chart for the server or a specific member.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        resolution="Time resolution for the chart buckets.",
        member="Show activity for this member only (default: whole server).",
        channel="Filter activity to a specific channel.",
        mode="Chart messages or XP earned (default: messages).",
    )
    async def activity(
        interaction: discord.Interaction,
        resolution: Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"] = "day",
        member: discord.User | None = None,
        channel: discord.TextChannel | None = None,
        mode: Literal["messages", "xp"] = "messages",
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        utc_offset = ctx.tz_offset_hours
        window_label = _WINDOW_LABELS[resolution]
        if utc_offset:
            tz_label = f"UTC{utc_offset:+g}"
        else:
            tz_label = "UTC"

        mode_label = "XP" if mode == "xp" else "Activity"
        if member is not None and channel is not None:
            title = f"{member.display_name} in #{channel.name} — {mode_label} ({window_label}, {tz_label})"
        elif member is not None:
            title = f"{member.display_name} — {mode_label} ({window_label}, {tz_label})"
        elif channel is not None:
            title = f"#{channel.name} — {mode_label} ({window_label}, {tz_label})"
        else:
            title = f"{guild.name} — {mode_label} ({window_label}, {tz_label})"

        user_id = member.id if member is not None else None
        channel_id = channel.id if channel is not None else None

        if mode == "xp":
            def _query_xp():
                with ctx.open_db() as conn:
                    if resolution in ("hour_of_day", "day_of_week"):
                        _labels, _xp_totals = query_xp_histogram(
                            conn, guild.id, cast(Literal["hour_of_day", "day_of_week"], resolution),
                            user_id=user_id, channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return _labels, _xp_totals, [], False
                    else:
                        _labels, _xp_totals, _member_counts = query_xp_activity(
                            conn, guild.id, resolution, user_id=user_id, channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return _labels, _xp_totals, _member_counts, member is None and channel is None
            labels, counts, member_counts, show_members = await asyncio.to_thread(_query_xp)
            y_label = "XP Earned"
            bar_label = "XP"
            empty_msg = f"No XP activity recorded for the {window_label.lower()}."
        else:
            def _query_activity():
                with ctx.open_db() as conn:
                    if resolution in ("hour_of_day", "day_of_week"):
                        _labels, _msg_counts = query_message_histogram(
                            conn, guild.id, cast(Literal["hour_of_day", "day_of_week"], resolution),
                            user_id=user_id, channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return _labels, _msg_counts, [], False
                    else:
                        _labels, _msg_counts, _member_counts = query_message_activity(
                            conn, guild.id, resolution, user_id=user_id, channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return _labels, _msg_counts, _member_counts, member is None and channel is None
            labels, counts, member_counts, show_members = await asyncio.to_thread(_query_activity)
            y_label = "Messages"
            bar_label = "Messages"
            empty_msg = f"No message activity recorded for the {window_label.lower()}."

        if not any(c > 0 for c in counts):
            await interaction.followup.send(empty_msg, ephemeral=True)
            return

        chart_bytes = await asyncio.to_thread(
            render_activity_chart,
            labels, counts, member_counts,
            title=title, resolution=resolution, show_members=show_members,
            y_label=y_label, bar_label=bar_label,
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

    def _vs_baseline(p: DropoffProfile, baseline_label: str = "server") -> str:
        """User's msg drop relative to the baseline trend."""
        srv_pct = ((p.server_msgs_recent - p.server_msgs_prev) / p.server_msgs_prev * 100
                   if p.server_msgs_prev else 0.0)
        usr_pct = ((p.msgs_recent - p.msgs_prev) / p.msgs_prev * 100
                   if p.msgs_prev else 0.0)
        diff = round(usr_pct - srv_pct)
        return f"{diff:+d}pp vs {baseline_label}"

    def _baseline_header(p: DropoffProfile, baseline_label: str = "Server") -> str:
        """One-line baseline trend shown above the ranked list."""
        return (
            f"{baseline_label} trend: **{_arrow(p.server_msgs_prev, p.server_msgs_recent)}** msgs"
            f" ({_pct(p.server_msgs_prev, p.server_msgs_recent)})\n"
        )

    def _fmt_compact(
        rank: int, p: DropoffProfile, guild: discord.Guild,
        baseline_label: str = "server",
    ) -> str:
        """Format one user as a name line plus a compact monospace table."""
        member = guild.get_member(p.user_id)
        name = member.mention if member else f"<@{p.user_id}>"
        lvl = f" Lv{p.level}" if p.level else ""

        voice_row = ""
        if p.voice_xp_prev or p.voice_xp_recent:
            voice_row = "\n" + _tbl_row("Voice XP", p.voice_xp_prev, p.voice_xp_recent, fmt=".0f")

        table = (
            _tbl_row("Msgs", p.msgs_prev, p.msgs_recent)
            + "\n" + _tbl_row("Days", p.days_prev, p.days_recent, suffix=f" /{p.days_in_window}")
            + voice_row
            + "\n" + _tbl_row("Channels", p.channels_prev, p.channels_recent)
            + "\n" + _tbl_row("Partners", p.partners_prev, p.partners_recent)
        )
        vs = _vs_baseline(p, baseline_label)

        return f"`{rank:>2}.` {name}{lvl} \u00b7 {_last_seen_str(p.last_seen_ts)} \u00b7 {vs}\n```\n{table}\n```"

    def _ch_name(guild: discord.Guild, cid: int) -> str:
        ch = guild.get_channel(cid)
        return f"#{ch.name}" if ch and hasattr(ch, "name") else f"#{cid}"

    def _tbl_row(label: str, prev: int | float, recent: int | float, fmt: str = "g", suffix: str = "") -> str:
        """One row of a compact comparison table (fits ~36 char mobile width)."""
        p = f"{prev:{fmt}}"
        r = f"{recent:{fmt}}"
        pct = _pct(prev, recent)
        return f"{label:<12s}{p:>5s}\u2192{r:>5s} {pct:>6s}{suffix}"

    def _fmt_detail(
        p: DropoffProfile, guild: discord.Guild, period_label: str,
        baseline_label: str = "server",
    ) -> discord.Embed:
        """Build a full-detail embed for a single user."""
        member = guild.get_member(p.user_id)
        name = member.display_name if member else f"User {p.user_id}"
        bl_title = baseline_label.capitalize()

        srv_trend = _pct(p.server_msgs_prev, p.server_msgs_recent)
        lvl_note = f"  \u00b7  Level {p.level} ({p.total_xp:,.0f} XP)" if p.level else ""
        embed = discord.Embed(
            title=f"Engagement Profile \u2014 {name}",
            description=(
                f"Comparing the prior {period_label} to the most recent {period_label}.{lvl_note}\n"
                f"{bl_title} trend: **{_arrow(p.server_msgs_prev, p.server_msgs_recent)}** msgs ({srv_trend})\n"
                f"Last seen: {_last_seen_str(p.last_seen_ts)}"
            ),
            color=discord.Color.orange(),
        )
        if member and member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        # Activity table
        header = f"{'':12s}{'Prior':>5s} {'Recnt':>5s} {'Chg':>6s}"
        activity_rows = [
            header,
            _tbl_row("Msgs", p.msgs_prev, p.msgs_recent),
            _tbl_row("Days", p.days_prev, p.days_recent, suffix=f" /{p.days_in_window}"),
            _tbl_row("Channels", p.channels_prev, p.channels_recent),
        ]
        extras = f"\n{_vs_baseline(p, baseline_label)}"
        if p.longest_gap_secs > 0:
            extras += f" \u00b7 Silence: {_gap_str(p.longest_gap_secs)}"
        if p.first_activity_day is not None and p.first_activity_day > 0:
            extras += f"\nFirst active: day {p.first_activity_day}/{p.days_in_window}"
        embed.add_field(
            name="Activity",
            value=f"```\n{chr(10).join(activity_rows)}\n```{extras}",
            inline=False,
        )

        # XP table
        xp_rows = [header]
        if p.text_xp_prev or p.text_xp_recent:
            xp_rows.append(_tbl_row("Text XP", p.text_xp_prev, p.text_xp_recent, fmt=".0f"))
        if p.reply_xp_prev or p.reply_xp_recent:
            xp_rows.append(_tbl_row("Reply XP", p.reply_xp_prev, p.reply_xp_recent, fmt=".0f"))
        if p.voice_xp_prev or p.voice_xp_recent:
            xp_rows.append(_tbl_row("Voice XP", p.voice_xp_prev, p.voice_xp_recent, fmt=".0f"))
        if p.image_react_xp_prev or p.image_react_xp_recent:
            xp_rows.append(_tbl_row("Img Rx XP", p.image_react_xp_prev, p.image_react_xp_recent, fmt=".0f"))
        if len(xp_rows) > 1:
            embed.add_field(
                name="XP Breakdown",
                value=f"```\n{chr(10).join(xp_rows)}\n```",
                inline=False,
            )

        # Conversations table
        reply_pct_prev = round(p.replies_prev / p.msgs_prev * 100) if p.msgs_prev else 0
        reply_pct_recent = round(p.replies_recent / p.msgs_recent * 100) if p.msgs_recent else 0
        convo_rows = [
            header,
            _tbl_row("Replies", p.replies_prev, p.replies_recent),
            _tbl_row("Initiations", p.initiations_prev, p.initiations_recent),
            _tbl_row("Avg len", round(p.avg_len_prev), round(p.avg_len_recent)),
        ]
        if p.deep_convos_prev or p.deep_convos_recent:
            convo_rows.append(_tbl_row("Deep threads", p.deep_convos_prev, p.deep_convos_recent))
        extras_c = f"\nReply rate: {reply_pct_prev}% \u2192 {reply_pct_recent}%"
        embed.add_field(
            name="Conversations",
            value=f"```\n{chr(10).join(convo_rows)}\n```{extras_c}",
            inline=False,
        )

        # Social table
        social_rows = [
            header,
            _tbl_row("Partners", p.partners_prev, p.partners_recent),
            _tbl_row("Inbound @s", p.inbound_prev, p.inbound_recent),
            _tbl_row("Outbound @s", p.outbound_prev, p.outbound_recent),
        ]
        embed.add_field(
            name="Social",
            value=f"```\n{chr(10).join(social_rows)}\n```",
            inline=False,
        )

        # Content table
        content_rows = [header]
        if p.attachments_prev or p.attachments_recent:
            content_rows.append(_tbl_row("Attachments", p.attachments_prev, p.attachments_recent))
        if p.reactions_prev or p.reactions_recent:
            rpm_prev = p.reactions_prev / p.msgs_prev if p.msgs_prev else 0
            rpm_recent = p.reactions_recent / p.msgs_recent if p.msgs_recent else 0
            content_rows.append(_tbl_row("Reactions", p.reactions_prev, p.reactions_recent))
            content_rows.append(f"{'  per msg':<12s}{rpm_prev:>5.1f}{rpm_recent:>6.1f}")
        if len(content_rows) > 1:
            embed.add_field(
                name="Content",
                value=f"```\n{chr(10).join(content_rows)}\n```",
                inline=False,
            )

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
                migration_lines.append(f"Active: {stayed_names}")
            embed.add_field(name="Channel Migration", value="\n".join(migration_lines), inline=False)

        # Patterns table
        pattern_rows = []
        if p.peak_hour_prev is not None or p.peak_hour_recent is not None:
            h_prev = _HOD_LABELS[p.peak_hour_prev] if p.peak_hour_prev is not None else "\u2014"
            h_recent = _HOD_LABELS[p.peak_hour_recent] if p.peak_hour_recent is not None else "\u2014"
            pattern_rows.append(f"{'Peak hour':<12s}{h_prev:>5s}\u2192{h_recent:>5s}")
        if p.msgs_prev or p.msgs_recent:
            pattern_rows.append(f"{'Weekday %':<12s}{p.weekday_pct_prev:>4.0f}%\u2192{p.weekday_pct_recent:>4.0f}%")
        if pattern_rows:
            embed.add_field(
                name="Patterns",
                value=f"```\n{chr(10).join(pattern_rows)}\n```",
                inline=False,
            )

        return embed

    # ── command ───────────────────────────────────────────────────────────

    @bot.tree.command(
        name="dropoff",
        description="Show members with the largest drop in engagement between two equal time windows.",
    )
    @app_commands.default_permissions(manage_guild=True)
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
        member: discord.User | None = None,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
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

        _target_uid = member.id if member else None

        def _query_dropoff():
            with ctx.open_db() as conn:
                return query_dropoff_profiles(
                    conn, guild.id, period_secs,
                    channel_id=channel_id, limit=limit,
                    target_user_id=_target_uid,
                )
        profiles = await asyncio.to_thread(_query_dropoff)

        # ── detail view (single member) ───────────────────────────────────
        if member is not None:
            if not profiles:
                await interaction.followup.send(
                    f"No message data found for {member.mention} in the last two {period_label}s.",
                    ephemeral=True,
                )
                return
            bl = f"#{channel.name}" if channel else "server"
            embed = _fmt_detail(profiles[0], guild, period_label, baseline_label=bl)
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

        bl = f"#{channel.name}" if channel else "Server"
        header = _baseline_header(profiles[0], baseline_label=bl)
        lines = [
            _fmt_compact(rank, p, guild, baseline_label=bl.lower())
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
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member to profile.")
    async def session_burst(
        interaction: discord.Interaction,
        member: discord.User,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        _member_id = member.id

        def _query_burst():
            with ctx.open_db() as conn:
                return query_session_burst(conn, guild.id, _member_id)
        pre_sessions, post_sessions, overall_rate = await asyncio.to_thread(_query_burst)

        if not post_sessions:
            await interaction.followup.send(
                f"Not enough message history recorded for {member.mention} "
                "to build a session profile.",
                ephemeral=True,
            )
            return

        chart_bytes = await asyncio.to_thread(
            render_session_burst_chart,
            pre_sessions, post_sessions, overall_rate,
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
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        limit="Number of members to show at each end (1–15, default 5).",
    )
    async def burst_ranking(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 15] = 5,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        def _query_ranking():
            with ctx.open_db() as conn:
                return query_burst_ranking(conn, guild.id)
        ranking = await asyncio.to_thread(_query_ranking)

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

        chart_bytes = await asyncio.to_thread(
            render_burst_ranking_chart, entries, limit=limit, guild_name=guild.name,
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="burst_ranking.png"),
            ephemeral=True,
        )
