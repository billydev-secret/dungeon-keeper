from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.activity_graphs import Resolution, query_role_growth, render_role_growth_chart
from services.auto_delete_service import parse_duration_seconds

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
        with ctx.open_db() as conn:
            activities = ctx.get_member_last_activity_map(conn, guild.id, role_member_ids)

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

        with ctx.open_db() as conn:
            activities = ctx.get_member_last_activity_map(conn, guild.id, sfw_member_ids)

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
    @app_commands.describe(time_period="How long without a message counts as inactive, e.g. 7d, 2h, 30m")
    async def inactive(interaction: discord.Interaction, time_period: str):
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

        with ctx.open_db() as conn:
            activities = ctx.get_member_last_activity_map(conn, guild.id, all_member_ids)

        inactive_members = sorted(
            [m for m in all_members if activities.get(m.id) is None or activities[m.id].created_at < cutoff_ts],
            key=lambda m: (activities[m.id].created_at if m.id in activities else 0),
        )

        total = len(all_members)
        inactive_count = len(inactive_members)
        percent = (inactive_count / total * 100) if total else 0
        summary = (
            f"**Inactive Members Report ({time_period})**\n"
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

    @report_group.command(name="role_growth", description="Chart cumulative role grants over time.")
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

        with ctx.open_db() as conn:
            labels, role_counts = query_role_growth(conn, ctx.guild_id, res)

        if roles is not None:
            wanted = {r.strip().lower() for r in roles.split(",") if r.strip()}
            role_counts = {
                name: counts for name, counts in role_counts.items()
                if name.lower() in wanted
            }

        if not role_counts:
            await interaction.followup.send("No role grant history recorded yet.", ephemeral=True)
            return

        chart_bytes = render_role_growth_chart(
            labels,
            role_counts,
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
        }[res]

        channel_id = channel.id if channel else None
        scope = f" in #{channel.name}" if channel else ""

        with ctx.open_db() as conn:
            buckets = query_message_cadence(
                conn, ctx.guild_id, res,
                utc_offset_hours=ctx.tz_offset_hours,
                channel_id=channel_id,
            )

        if not buckets or all(b.median_gap == 0 for b in buckets):
            await interaction.followup.send(f"No message data found{scope} for this period.", ephemeral=True)
            return

        chart_bytes = render_message_cadence_chart(
            buckets,
            title=f"Message Cadence{scope} — {window_label}",
        )
        await interaction.followup.send(
            file=discord.File(fp=__import__("io").BytesIO(chart_bytes), filename="message_cadence.png"),
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

        with ctx.open_db() as conn:
            # Get XP levels for candidates
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

            # Filter to level > 5
            eligible = [m for m in candidates if levels.get(m.id, (1, 0))[0] > 5]
            if not eligible:
                await interaction.followup.send(
                    "No members above level 5 without spicy access.", ephemeral=True
                )
                return

            eligible_ids = [m.id for m in eligible]

            # Check for NSFW removal events in role_events
            nsfw_role_name = nsfw_role.name if nsfw_role else ""
            pruned_users: dict[int, float] = {}  # user_id -> removal timestamp
            if nsfw_role_name:
                for i in range(0, len(eligible_ids), batch_size):
                    batch = eligible_ids[i:i + batch_size]
                    placeholders = ", ".join("?" for _ in batch)
                    # Get the most recent event per user for the NSFW role
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

            # Get activity data
            activities = ctx.get_member_last_activity_map(conn, guild.id, eligible_ids)

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

    bot.tree.add_command(report_group)

