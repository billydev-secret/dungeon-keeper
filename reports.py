from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.activity_graphs import (
    MessageRateWindow,
    Resolution,
    query_greeter_response_times,
    query_message_rate_10min,
    query_role_growth,
    render_greeter_response_chart,
    render_message_rate_chart,
    render_role_growth_chart,
)
from services.auto_delete_service import parse_duration_seconds
from xp_system import log_role_event

if TYPE_CHECKING:
    from app_context import AppContext, Bot

SAFE_TEXT_CHUNK = 1900


def chunk_text(text: str, limit: int = SAFE_TEXT_CHUNK) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


async def send_ephemeral_text(interaction: discord.Interaction, text: str) -> None:
    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)


def format_member_activity_line(member: discord.Member, activity) -> str:
    if activity is None:
        return f"{member.display_name} - no recorded message yet"

    created_at = int(activity.created_at)
    if getattr(activity, "channel_id", 0) <= 0:
        return (
            f"{member.display_name} - last seen <t:{created_at}:R> "
            f"(<t:{created_at}:f>)"
        )
    return (
        f"{member.display_name} - last seen <t:{created_at}:R> "
        f"(<t:{created_at}:f>) in <#{activity.channel_id}>"
    )


def register_reports(bot: Bot, ctx: AppContext) -> None:
    report_group = app_commands.Group(
        name="report",
        description="Member activity and role membership reports.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @report_group.command(name="list_role", description="List members who currently have a role.")
    @app_commands.describe(role="The role to inspect")
    async def list_role(interaction: discord.Interaction, role: discord.Role):
        if not role.members:
            await interaction.response.send_message(f"No members found in **{role.name}**.", ephemeral=True)
            return
        output = "\n".join(member.display_name for member in role.members)
        if len(output) > 1900:
            output = output[:1900] + "\n... (truncated)"
        await interaction.response.send_message(f"**Members in {role.name}:**\n{output}", ephemeral=True)

    @report_group.command(name="inactive_role", description="Report role members inactive for N days.")
    @app_commands.describe(role="Role to analyze", days="Number of days to check (default 7)")
    async def inactive_role(
        interaction: discord.Interaction, role: discord.Role, days: app_commands.Range[int, 1, 60] = 7
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command only works in a server.", ephemeral=True)
            return
        cutoff = discord.utils.utcnow() - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        role_members = sorted(role.members, key=lambda current: current.display_name.lower())
        role_member_ids = [current.id for current in role_members]

        def _fetch_inactive_role():
            with ctx.open_db() as conn:
                return ctx.get_member_last_activity_map(conn, guild.id, role_member_ids)
        activities = await asyncio.to_thread(_fetch_inactive_role)

        inactive_members = [
            current
            for current in role_members
            if activities.get(current.id) is None or activities[current.id].created_at < cutoff_ts
        ]
        total = len(role_members)
        inactive_count = len(inactive_members)
        percent = (inactive_count / total * 100) if total else 0
        summary = (
            f"**Role Activity Report -- {role.name} ({days} days)**\n"
            f"Total Members: {total}\n"
            f"Inactive: {inactive_count} ({percent:.1f}%)\n"
            f"Tracking Coverage: {len(activities)}/{total}\n"
            f"----------------------------------\n"
        )
        if inactive_members:
            block = "\n".join(
                format_member_activity_line(current, activities.get(current.id))
                for current in inactive_members
            )
            summary += "\n**Inactive Members:**\n" + block
        else:
            summary += "\nAll members active in this period."
        if any(current.id not in activities for current in inactive_members):
            summary += (
                "\n\nSome members have no recorded message yet because activity tracking "
                "starts after this version is deployed."
            )
        await send_ephemeral_text(interaction, summary)

    @report_group.command(
        name="oldest_sfw",
        description="Show members without spicy access who have the oldest last messages.",
    )
    @app_commands.describe(count="How many members to show (default 10)")
    async def oldest_sfw(
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 50] = 10,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command only works in a server.", ephemeral=True)
            return

        nsfw_cfg = ctx.grant_roles.get("nsfw")
        nsfw_role_id = nsfw_cfg["role_id"] if nsfw_cfg else 0
        nsfw_role = guild.get_role(nsfw_role_id) if nsfw_role_id else None
        sfw_members = [
            m for m in guild.members
            if not m.bot and (nsfw_role is None or nsfw_role not in m.roles)
        ]
        sfw_member_ids = [m.id for m in sfw_members]

        def _fetch_oldest_sfw():
            with ctx.open_db() as conn:
                return ctx.get_member_last_activity_map(conn, guild.id, sfw_member_ids)
        activities = await asyncio.to_thread(_fetch_oldest_sfw)

        sorted_members = sorted(
            sfw_members,
            key=lambda m: activities[m.id].created_at if m.id in activities else 0,
        )
        top = sorted_members[:count]

        nsfw_role_label = nsfw_role.name if nsfw_role else "spicy role (not configured)"
        header = (
            f"**Oldest SFW Members (no {nsfw_role_label}) — top {count}**\n"
            f"Total without spicy access: {len(sfw_members)}\n"
            f"----------------------------------\n"
        )
        block = "\n".join(format_member_activity_line(m, activities.get(m.id)) for m in top)
        await send_ephemeral_text(interaction, header + block)

    @report_group.command(name="inactive", description="Show all server members inactive for a given period.")
    @app_commands.describe(
        time_period="How long without a message counts as inactive, e.g. 7d, 2h, 30m",
        channel="Only count activity in this channel.",
        exclude_gif_only="Ignore members whose only recent activity is GIF/image links.",
    )
    async def inactive(
        interaction: discord.Interaction,
        time_period: str,
        channel: discord.TextChannel | None = None,
        exclude_gif_only: bool = False,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        seconds = parse_duration_seconds(time_period)
        if seconds is None:
            await interaction.response.send_message(
                "Invalid time period. Use a value like `7d`, `2h`, `30m`, or `1d12h`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        cutoff_ts = (discord.utils.utcnow() - timedelta(seconds=seconds)).timestamp()
        all_members = [m for m in guild.members if not m.bot]
        all_member_ids = [m.id for m in all_members]

        if channel is not None or exclude_gif_only:
            from services.message_store import query_last_substantive_activity
            _ch_id = channel.id if channel else None

            def _fetch_inactive():
                with ctx.open_db() as conn:
                    return query_last_substantive_activity(
                        conn, guild.id, all_member_ids,
                        channel_id=_ch_id,
                        exclude_gif_only=exclude_gif_only,
                    )
        else:
            def _fetch_inactive():
                with ctx.open_db() as conn:
                    return ctx.get_member_last_activity_map(conn, guild.id, all_member_ids)
        activities = await asyncio.to_thread(_fetch_inactive)

        inactive_members = sorted(
            [m for m in all_members if activities.get(m.id) is None or activities[m.id].created_at < cutoff_ts],
            key=lambda m: (activities[m.id].created_at if m.id in activities else 0),
        )

        total = len(all_members)
        inactive_count = len(inactive_members)
        percent = (inactive_count / total * 100) if total else 0
        filters: list[str] = []
        if channel:
            filters.append(f"in #{channel.name}")
        if exclude_gif_only:
            filters.append("excl. GIF-only")
        filter_label = f" ({', '.join(filters)})" if filters else ""
        summary = (
            f"**Inactive Members Report ({time_period}{filter_label})**\n"
            f"Total Members: {total}\n"
            f"Inactive: {inactive_count} ({percent:.1f}%)\n"
            f"----------------------------------\n"
        )
        if inactive_members:
            block = "\n".join(
                format_member_activity_line(m, activities.get(m.id))
                for m in inactive_members
            )
            summary += "\n**Inactive Members:**\n" + block
        else:
            summary += "\nAll members have been active in this period."
        if any(m.id not in activities for m in inactive_members):
            summary += (
                "\n\nSome members have no recorded message yet because activity tracking "
                "starts after this version is deployed."
            )
        await send_ephemeral_text(interaction, summary)

    @report_group.command(name="role_growth", description="Chart role membership over time.")
    @app_commands.describe(
        resolution="Time resolution: day (30d), week (12wk), month (12mo)",
        roles="Comma-separated role names to include (default: all)",
    )
    @app_commands.choices(resolution=[
        app_commands.Choice(name="Daily (last 30 days)", value="day"),
        app_commands.Choice(name="Weekly (last 12 weeks)", value="week"),
        app_commands.Choice(name="Monthly (last 12 months)", value="month"),
    ])
    async def role_growth(
        interaction: discord.Interaction,
        resolution: app_commands.Choice[str] | None = None,
        roles: str | None = None,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        res: Resolution = resolution.value if resolution else "week"  # type: ignore[assignment]
        window_label = {"day": "Last 30 Days", "week": "Last 12 Weeks", "month": "Last 12 Months"}[res]

        def _query_role_growth():
            with ctx.open_db() as conn:
                return query_role_growth(conn, ctx.guild_id, res)
        labels, role_counts = await asyncio.to_thread(_query_role_growth)

        if roles is not None:
            wanted = {r.strip().lower() for r in roles.split(",") if r.strip()}
            role_counts = {
                name: counts for name, counts in role_counts.items()
                if name.lower() in wanted
            }

        if not role_counts:
            await interaction.followup.send("No role grant history recorded yet.", ephemeral=True)
            return

        chart_bytes = await asyncio.to_thread(
            render_role_growth_chart, labels, role_counts,
            title=f"Role Growth — {window_label}",
        )
        await interaction.followup.send(
            file=discord.File(fp=__import__("io").BytesIO(chart_bytes), filename="role_growth.png"),
            ephemeral=True,
        )

    @report_group.command(
        name="message_cadence",
        description="Chart average, mode, and 80th percentile time between messages.",
    )
    @app_commands.describe(
        resolution="Time resolution: hourly (24h), daily (30d), weekly (12wk), monthly (12mo)",
        channel="Restrict to a specific channel.",
    )
    @app_commands.choices(resolution=[
        app_commands.Choice(name="Hourly (last 24 hours)", value="hour"),
        app_commands.Choice(name="Daily (last 30 days)", value="day"),
        app_commands.Choice(name="Weekly (last 12 weeks)", value="week"),
        app_commands.Choice(name="Monthly (last 12 months)", value="month"),
        app_commands.Choice(name="By Hour of Day", value="hour_of_day"),
        app_commands.Choice(name="By Day of Week", value="day_of_week"),
    ])
    async def message_cadence(
        interaction: discord.Interaction,
        resolution: app_commands.Choice[str] | None = None,
        channel: discord.TextChannel | None = None,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from services.activity_graphs import query_message_cadence, render_message_cadence_chart

        res: Resolution = resolution.value if resolution else "day"  # type: ignore[assignment]
        window_label = {
            "hour": "Last 24 Hours", "day": "Last 30 Days",
            "week": "Last 12 Weeks", "month": "Last 12 Months",
            "hour_of_day": "By Hour of Day", "day_of_week": "By Day of Week",
        }[res]

        channel_id = channel.id if channel else None
        scope = f" in #{channel.name}" if channel else ""

        def _query_cadence():
            with ctx.open_db() as conn:
                return query_message_cadence(
                    conn, ctx.guild_id, res,
                    utc_offset_hours=ctx.tz_offset_hours,
                    channel_id=channel_id,
                )
        buckets = await asyncio.to_thread(_query_cadence)

        if not buckets or all(b.median_gap == 0 for b in buckets):
            await interaction.followup.send(f"No message data found{scope} for this period.", ephemeral=True)
            return

        chart_bytes = await asyncio.to_thread(
            render_message_cadence_chart, buckets,
            title=f"Message Cadence{scope} — {window_label}",
        )
        await interaction.followup.send(
            file=discord.File(fp=__import__("io").BytesIO(chart_bytes), filename="message_cadence.png"),
            ephemeral=True,
        )

    @report_group.command(
        name="join_times",
        description="Histogram of when members joined the server.",
    )
    @app_commands.describe(
        resolution="Group by hour of day or day of week.",
    )
    @app_commands.choices(resolution=[
        app_commands.Choice(name="By Hour of Day", value="hour_of_day"),
        app_commands.Choice(name="By Day of Week", value="day_of_week"),
    ])
    async def join_times(
        interaction: discord.Interaction,
        resolution: app_commands.Choice[str] | None = None,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_guild:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from datetime import datetime, timezone as tz

        from services.activity_graphs import render_join_histogram

        res = resolution.value if resolution else "hour_of_day"
        offset_secs = int(ctx.tz_offset_hours * 3600)

        _DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        _HOD_LABELS = [
            "12am", "1am", "2am", "3am", "4am", "5am", "6am", "7am",
            "8am", "9am", "10am", "11am", "12pm", "1pm", "2pm", "3pm",
            "4pm", "5pm", "6pm", "7pm", "8pm", "9pm", "10pm", "11pm",
        ]

        if res == "hour_of_day":
            labels, n_bins = _HOD_LABELS, 24
        else:
            labels, n_bins = _DOW_LABELS, 7

        counts = [0] * n_bins
        for m in guild.members:
            if m.bot or m.joined_at is None:
                continue
            dt = m.joined_at.astimezone(tz.utc)
            ts = dt.timestamp() + offset_secs
            local_dt = datetime.fromtimestamp(ts, tz=tz.utc)
            if res == "hour_of_day":
                counts[local_dt.hour] += 1
            else:
                counts[(local_dt.weekday() + 1) % 7] += 1

        title_label = "By Hour of Day" if res == "hour_of_day" else "By Day of Week"
        chart_bytes = await asyncio.to_thread(
            render_join_histogram, labels, counts, f"Member Joins — {title_label}",
        )

        import io as _io
        await interaction.followup.send(
            file=discord.File(fp=_io.BytesIO(chart_bytes), filename="join_times.png"),
            ephemeral=True,
        )

    @report_group.command(
        name="promotion_review",
        description="Members above level 5 without spicy access — flags inactivity-pruned users.",
    )
    async def promotion_review(interaction: discord.Interaction):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        nsfw_cfg = ctx.grant_roles.get("nsfw")
        nsfw_role_id = nsfw_cfg["role_id"] if nsfw_cfg else 0
        nsfw_role = guild.get_role(nsfw_role_id) if nsfw_role_id else None
        # Members without the NSFW role (excluding bots)
        candidates = [
            m for m in guild.members
            if not m.bot and (nsfw_role is None or nsfw_role not in m.roles)
        ]
        if not candidates:
            await interaction.followup.send("No members found without spicy access.", ephemeral=True)
            return

        candidate_ids = [m.id for m in candidates]

        nsfw_role_name = nsfw_role.name if nsfw_role else ""

        def _query_promotion():
            with ctx.open_db() as conn:
                levels: dict[int, tuple[int, float]] = {}
                batch_size = 800
                for i in range(0, len(candidate_ids), batch_size):
                    batch = candidate_ids[i:i + batch_size]
                    placeholders = ", ".join("?" for _ in batch)
                    rows = conn.execute(
                        f"SELECT user_id, level, total_xp FROM member_xp "
                        f"WHERE guild_id = ? AND user_id IN ({placeholders})",
                        [guild.id, *batch],
                    ).fetchall()
                    for row in rows:
                        levels[int(row["user_id"])] = (int(row["level"]), float(row["total_xp"]))

                eligible_ids = [m.id for m in candidates if levels.get(m.id, (1, 0))[0] > 5]
                if not eligible_ids:
                    return levels, {}, {}

                pruned_users: dict[int, float] = {}
                if nsfw_role_name:
                    for i in range(0, len(eligible_ids), batch_size):
                        batch = eligible_ids[i:i + batch_size]
                        placeholders = ", ".join("?" for _ in batch)
                        rows = conn.execute(
                            f"SELECT user_id, action, granted_at FROM role_events "
                            f"WHERE guild_id = ? AND role_name = ? "
                            f"AND user_id IN ({placeholders}) "
                            f"ORDER BY granted_at DESC",
                            [guild.id, nsfw_role_name, *batch],
                        ).fetchall()
                        seen: set[int] = set()
                        for row in rows:
                            uid = int(row["user_id"])
                            if uid not in seen:
                                seen.add(uid)
                                if row["action"] == "remove":
                                    pruned_users[uid] = float(row["granted_at"])

                activities = ctx.get_member_last_activity_map(conn, guild.id, eligible_ids)
                return levels, pruned_users, activities

        levels, pruned_users, activities = await asyncio.to_thread(_query_promotion)

        eligible = [m for m in candidates if levels.get(m.id, (1, 0))[0] > 5]
        if not eligible:
            await interaction.followup.send(
                "No members above level 5 without spicy access.", ephemeral=True
            )
            return

        # Sort by level descending, then XP descending
        eligible.sort(key=lambda m: (-levels.get(m.id, (1, 0))[0], -levels.get(m.id, (1, 0))[1]))

        nsfw_label = nsfw_role.name if nsfw_role else "spicy role (not configured)"
        header = (
            f"**Promotion Review — above level 5, no {nsfw_label}**\n"
            f"Total eligible: {len(eligible)}\n"
            f"----------------------------------\n"
        )

        lines: list[str] = []
        for m in eligible:
            lvl, xp = levels.get(m.id, (1, 0.0))
            activity = activities.get(m.id)
            if activity is not None:
                ts = int(activity.created_at)
                last_seen = f"last seen <t:{ts}:R>"
            else:
                last_seen = "no recorded activity"

            line = f"**{m.display_name}** — Level {lvl} ({xp:.1f} XP) — {last_seen}"
            if m.id in pruned_users:
                removal_ts = int(pruned_users[m.id])
                line += f"\n  ⚠ Previously had {nsfw_label}, removed <t:{removal_ts}:R> (inactivity sweep)"
            lines.append(line)

        await send_ephemeral_text(interaction, header + "\n".join(lines))

    # ------------------------------------------------------------------
    # /report quality_scores
    # ------------------------------------------------------------------

    @report_group.command(
        name="quality_scores",
        description="Ranked member quality scores with component breakdowns.",
    )
    @app_commands.describe(limit="Number of scored members to show (default 10).")
    async def quality_scores(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 100] = 10,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from services.member_quality_score import (
            STATUS_ACTIVE,
            STATUS_INSUFFICIENT,
            STATUS_LEAVE,
            STATUS_ONBOARDING,
            compute_quality_scores,
        )

        _members = list(guild.members)

        def _compute():
            with ctx.open_db() as conn:
                return compute_quality_scores(conn, guild.id, _members)
        scores = await asyncio.to_thread(_compute)

        if not scores:
            await interaction.followup.send("No members to score.", ephemeral=True)
            return

        active = [s for s in scores if s.status == STATUS_ACTIVE]
        onboarding = [s for s in scores if s.status == STATUS_ONBOARDING]
        insufficient = [s for s in scores if s.status == STATUS_INSUFFICIENT]
        on_leave = [s for s in scores if s.status == STATUS_LEAVE]
        shown = list(reversed(active))[:limit]
        now_ts = discord.utils.utcnow().timestamp()

        # -- Summary line --
        extras: list[str] = []
        if onboarding:
            extras.append(f"{len(onboarding)} onboarding")
        if insufficient:
            extras.append(f"{len(insufficient)} insufficient")
        if on_leave:
            extras.append(f"{len(on_leave)} on leave")
        summary = f"Bottom **{len(shown)}** of {len(active)} scored"
        if extras:
            summary += " \u00b7 " + " \u00b7 ".join(extras)

        # -- Table --
        import unicodedata

        def _mono(text: str, width: int) -> str:
            """Fixed visual-width string safe for monospace alignment."""
            out: list[str] = []
            w = 0
            for ch in text:
                if ord(ch) > 0xFFFF or ch in "\ufe0f\ufe0e\u200d":
                    continue
                if unicodedata.category(ch) == "So":
                    continue
                cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                if w + cw > width:
                    break
                out.append(ch)
                w += cw
            name = "".join(out).strip() or "?"
            vw = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in name)
            return name + " " * (width - vw)

        COL = 14
        ACT_W = 7
        tbl: list[str] = []
        hdr = f" # {_mono('Member', COL)} {'Tot':>3} {'Eng':>3} {'C&R':>3} {'Res':>3} {'Pst':>3} {'Seen':>{ACT_W}}"
        tbl.append(hdr)
        tbl.append("\u2500" * len(hdr))

        for rank, s in enumerate(shown, 1):
            m = guild.get_member(s.user_id)
            is_new = m is not None and m.joined_at is not None and (discord.utils.utcnow() - m.joined_at).days < 30
            raw = m.display_name if m else f"User {s.user_id}"
            if is_new:
                raw = f"*{raw}"
            name = _mono(raw, COL)
            tot = min(round(s.final_score * 100), 100)
            eng = min(round(s.engagement_given * 100), 100)
            cr = min(round(s.consistency_recency * 100), 100)
            res = min(round(s.content_resonance * 100), 100)
            pst = min(round(s.posting_activity * 100), 100)
            if s.last_active_ts > 0:
                days_ago = int((now_ts - s.last_active_ts) / 86400)
                last = f"{days_ago}d" if days_ago > 0 else "0d"
            else:
                last = "\u2014"
            if s.tenure_buffer_days > 0:
                last += f"+{s.tenure_buffer_days}"
            tbl.append(
                f"{rank:>2} {name} {tot:>3} {eng:>3} {cr:>3} {res:>3} {pst:>3} {last:>{ACT_W}}")

        table_text = "```\n" + "\n".join(tbl) + "\n```"

        # -- Build embed --
        embed = discord.Embed(
            title="Member Quality Scores",
            description=summary + "\n" + table_text,
            color=discord.Color.blurple(),
        )

        # Compact footer sections
        footer_parts: list[str] = []
        def _name(uid: int) -> str:
            m = guild.get_member(uid)
            return m.display_name if m else f"User {uid}"

        if insufficient:
            names = ", ".join(_name(s.user_id) for s in insufficient[:5])
            if len(insufficient) > 5:
                names += f" +{len(insufficient) - 5} more"
            footer_parts.append(f"**Insufficient Data** ({len(insufficient)}): {names}")
        if on_leave:
            names = ", ".join(_name(s.user_id) for s in on_leave[:5])
            if len(on_leave) > 5:
                names += f" +{len(on_leave) - 5} more"
            footer_parts.append(f"**On Leave** ({len(on_leave)}): {names}")
        if onboarding:
            footer_parts.append(f"**Onboarding** ({len(onboarding)}): not yet scored")
        if footer_parts:
            embed.add_field(name="\u200b", value="\n".join(footer_parts), inline=False)

        embed.set_footer(text="* < 30d tenure \u00b7 Eng=Engagement \u00b7 C&R=Consistency & Recency \u00b7 Res=Resonance \u00b7 Pst=Posts \u00b7 Seen=Last Active")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /report nsfw_gender
    # ------------------------------------------------------------------

    @report_group.command(
        name="nsfw_gender",
        description="Chart channel posting broken down by gender.",
    )
    @app_commands.describe(
        resolution="Time resolution: daily (30d), weekly (12wk), monthly (12mo)",
        display="Chart style: stacked bars or ratio line chart.",
        media_only="Only count messages with image/video attachments (no GIFs).",
        channel="Limit to a specific channel (default: all NSFW channels).",
    )
    @app_commands.choices(
        resolution=[
            app_commands.Choice(name="Daily (last 30 days)", value="day"),
            app_commands.Choice(name="Weekly (last 12 weeks)", value="week"),
            app_commands.Choice(name="Monthly (last 12 months)", value="month"),
        ],
        display=[
            app_commands.Choice(name="Stacked bar chart", value="bar"),
            app_commands.Choice(name="Ratio line chart", value="line"),
        ],
    )
    async def nsfw_gender(
        interaction: discord.Interaction,
        resolution: app_commands.Choice[str] | None = None,
        display: app_commands.Choice[str] | None = None,
        media_only: bool = False,
        channel: discord.TextChannel | None = None,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from services.activity_graphs import (
            query_nsfw_gender_activity,
            render_nsfw_gender_chart,
            render_nsfw_gender_line_chart,
        )

        res: Resolution = resolution.value if resolution else "week"  # type: ignore[assignment]
        display_mode = display.value if display else "bar"
        window_label = {"day": "Last 30 Days", "week": "Last 12 Weeks", "month": "Last 12 Months"}[res]

        if channel is not None:
            target_channel_ids = [channel.id]
            channel_label = f"#{channel.name}"
        else:
            target_channel_ids = [
                ch.id for ch in guild.channels
                if getattr(ch, "nsfw", False)
            ]
            channel_label = "NSFW Channels"

        if not target_channel_ids:
            await interaction.followup.send("No matching channels found.", ephemeral=True)
            return

        def _query():
            with ctx.open_db() as conn:
                return query_nsfw_gender_activity(
                    conn, ctx.guild_id, res, target_channel_ids,
                    utc_offset_hours=ctx.tz_offset_hours,
                    media_only=media_only,
                )
        labels, gender_counts = await asyncio.to_thread(_query)

        if not gender_counts:
            await interaction.followup.send("No posting data found for this period.", ephemeral=True)
            return

        title_parts = [channel_label, "by Gender"]
        if media_only:
            title_parts.insert(1, "Media")
        title = f"{' '.join(title_parts)} \u2014 {window_label}"

        if display_mode == "line":
            renderer = render_nsfw_gender_line_chart
        else:
            renderer = render_nsfw_gender_chart

        chart_bytes = await asyncio.to_thread(
            renderer, labels, gender_counts, title=title,
        )
        await interaction.followup.send(
            file=discord.File(fp=__import__("io").BytesIO(chart_bytes), filename="nsfw_gender.png"),
            ephemeral=True,
        )

    @report_group.command(
        name="message_rate",
        description="Chart messages per 10-minute interval across the day.",
    )
    @app_commands.describe(
        window="Time window of data to average over.",
    )
    @app_commands.choices(window=[
        app_commands.Choice(name="Last 24 hours", value="day"),
        app_commands.Choice(name="Last 7 days", value="week"),
        app_commands.Choice(name="Last 30 days", value="month"),
    ])
    async def message_rate(
        interaction: discord.Interaction,
        window: app_commands.Choice[str] | None = None,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        win: MessageRateWindow = window.value if window else "week"  # type: ignore[assignment]
        window_label = {
            "day": "Last 24 Hours",
            "week": "Last 7 Days",
            "month": "Last 30 Days",
        }[win]
        tz_label = (
            f"UTC{ctx.tz_offset_hours:+g}" if ctx.tz_offset_hours else "UTC"
        )

        def _query():
            with ctx.open_db() as conn:
                return query_message_rate_10min(
                    conn, ctx.guild_id, win, utc_offset_hours=ctx.tz_offset_hours,
                )

        counts, days = await asyncio.to_thread(_query)

        if not any(c > 0 for c in counts):
            await interaction.followup.send(
                "No message activity recorded for the selected window.", ephemeral=True,
            )
            return

        chart_bytes = await asyncio.to_thread(
            render_message_rate_chart, counts, days,
            title=f"Message Rate \u2014 {window_label} ({tz_label})",
        )
        await interaction.followup.send(
            file=discord.File(fp=__import__("io").BytesIO(chart_bytes), filename="message_rate.png"),
            ephemeral=True,
        )

    @report_group.command(
        name="greeter_response",
        description="Chart how long new members wait for their first greeter message.",
    )
    @app_commands.describe(
        days="Only include joins from the last N days (default: all).",
    )
    async def greeter_response(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] | None = None,
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True,
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        if ctx.welcome_channel_id <= 0:
            await interaction.response.send_message(
                "No welcome channel is configured.", ephemeral=True,
            )
            return

        greeter_role = guild.get_role(ctx.greeter_role_id) if ctx.greeter_role_id else None
        if not greeter_role or not greeter_role.members:
            await interaction.response.send_message(
                "No greeter role is configured or the role has no members.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        greeter_ids = {m.id for m in greeter_role.members}

        # Build join times from invite_edges + current member joined_at
        cutoff_ts = 0.0
        if days is not None:
            cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

        def _query():
            join_times: dict[int, float] = {}

            with ctx.open_db() as conn:
                # Invite-tracked joins
                rows = conn.execute(
                    "SELECT invitee_id, joined_at FROM invite_edges WHERE guild_id = ? AND joined_at >= ?",
                    (guild.id, cutoff_ts),
                ).fetchall()
                for r in rows:
                    join_times[int(r[0])] = float(r[1])

                # Fill in current members whose joined_at is more accurate or missing from invite_edges
                for m in guild.members:
                    if m.bot or not m.joined_at:
                        continue
                    ts = m.joined_at.timestamp()
                    if ts >= cutoff_ts:
                        join_times[m.id] = ts

                return query_greeter_response_times(
                    conn, guild.id, ctx.welcome_channel_id, greeter_ids, join_times,
                )

        response_times = await asyncio.to_thread(_query)

        if not response_times:
            await interaction.followup.send(
                "No greeter response data found for the selected period.", ephemeral=True,
            )
            return

        window_label = f"Last {days} Days" if days else "All Time"
        chart_bytes = await asyncio.to_thread(
            render_greeter_response_chart, response_times,
            title=f"Greeter Response Time \u2014 {window_label}",
        )
        await interaction.followup.send(
            file=discord.File(fp=__import__("io").BytesIO(chart_bytes), filename="greeter_response.png"),
            ephemeral=True,
        )

    @report_group.command(
        name="backfill_roles",
        description="Sync role_events with current server state so the role growth graph is accurate.",
    )
    async def backfill_roles(interaction: discord.Interaction):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True,
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        def _backfill():
            grants_added = 0
            removes_added = 0
            now_ts = time.time()

            with ctx.open_db() as conn:
                # Build net state from role_events: {(user_id, role_name): net_count}
                rows = conn.execute(
                    """
                    SELECT user_id, role_name,
                           SUM(CASE WHEN action = 'grant' THEN 1 ELSE -1 END) AS net
                    FROM role_events
                    WHERE guild_id = ?
                    GROUP BY user_id, role_name
                    """,
                    (guild.id,),
                ).fetchall()
                db_state: dict[tuple[int, str], int] = {
                    (int(r[0]), str(r[1])): int(r[2]) for r in rows
                }

                # Current server truth: who actually has each role right now
                live_pairs: set[tuple[int, str]] = set()
                for role in guild.roles:
                    if role.is_default():
                        continue
                    for m in role.members:
                        live_pairs.add((m.id, role.name))

                # Members who have a role but DB doesn't reflect it — insert grant
                for user_id, role_name in live_pairs:
                    net = db_state.get((user_id, role_name), 0)
                    if net <= 0:
                        # Backdate to member join time if available
                        m = guild.get_member(user_id)
                        ts = m.joined_at.timestamp() if m and m.joined_at else now_ts
                        log_role_event(conn, guild.id, user_id, role_name, "grant", ts=ts)
                        grants_added += 1

                # Users the DB thinks have a role but they don't — insert remove
                for (user_id, role_name), net in db_state.items():
                    if net > 0 and (user_id, role_name) not in live_pairs:
                        log_role_event(conn, guild.id, user_id, role_name, "remove", ts=now_ts)
                        removes_added += 1

            return grants_added, removes_added

        grants_added, removes_added = await asyncio.to_thread(_backfill)

        await interaction.followup.send(
            f"Role backfill complete.\n"
            f"Grant events added: {grants_added}\n"
            f"Remove events added: {removes_added}",
            ephemeral=True,
        )

    bot.tree.add_command(report_group)

    # ------------------------------------------------------------------
    # /quality_leave group
    # ------------------------------------------------------------------

    leave_group = app_commands.Group(
        name="quality_leave",
        description="Manage leave of absence for quality scoring.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @leave_group.command(name="add", description="Put a member on leave of absence.")
    @app_commands.describe(
        member="Member to put on leave.",
        days="Duration of leave in days (30, 60, or 90).",
    )
    @app_commands.choices(days=[
        app_commands.Choice(name="30 days", value=30),
        app_commands.Choice(name="60 days", value=60),
        app_commands.Choice(name="90 days", value=90),
    ])
    async def leave_add(
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Choice[int],
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        from services.member_quality_score import add_leave

        now_ts = discord.utils.utcnow().timestamp()
        end_ts = now_ts + days.value * 86400
        with ctx.open_db() as conn:
            add_leave(conn, ctx.guild_id, member.id, now_ts, end_ts)

        await interaction.response.send_message(
            f"{member.mention} placed on leave of absence for {days.value} days.",
            ephemeral=True,
        )

    @leave_group.command(name="remove", description="Remove a member's leave of absence.")
    @app_commands.describe(member="Member to remove from leave.")
    async def leave_remove(interaction: discord.Interaction, member: discord.Member):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        from services.member_quality_score import remove_leave

        with ctx.open_db() as conn:
            removed = remove_leave(conn, ctx.guild_id, member.id)

        if removed:
            await interaction.response.send_message(
                f"{member.mention} removed from leave of absence.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{member.mention} was not on leave.", ephemeral=True
            )

    @leave_group.command(name="list", description="List all members on leave of absence.")
    async def leave_list(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        from services.member_quality_score import get_leaves

        with ctx.open_db() as conn:
            leaves = get_leaves(conn, ctx.guild_id)

        if not leaves:
            await interaction.response.send_message("No members on leave.", ephemeral=True)
            return

        now_ts = discord.utils.utcnow().timestamp()
        lines = ["**Members on Leave of Absence**\n"]
        for uid, (start_ts, end_ts) in leaves.items():
            m = guild.get_member(uid)
            name = m.display_name if m else f"User {uid}"
            remaining = max(0, int((end_ts - now_ts) / 86400))
            if end_ts < now_ts:
                lines.append(f"  {name} — **expired** (ended <t:{int(end_ts)}:R>)")
            else:
                lines.append(f"  {name} — {remaining}d remaining (ends <t:{int(end_ts)}:R>)")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    bot.tree.add_command(leave_group)

