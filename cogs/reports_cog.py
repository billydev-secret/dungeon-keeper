"""Report and quality-leave commands."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from reports import send_ephemeral_text

if TYPE_CHECKING:
    from app_context import AppContext, Bot


class ReportsCog(commands.Cog):
    report = app_commands.Group(
        name="report",
        description="Charts and tables about member activity, roles, and engagement.",
        default_permissions=discord.Permissions(manage_messages=True),
    )
    quality_leave = app_commands.Group(
        name="quality_leave",
        description="Pause quality scoring for members on leave of absence.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @report.command(
        name="promotion_review",
        description="Members past level 5 who still lack NSFW access. Flags pruned users.",
    )
    async def promotion_review(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        nsfw_cfg = ctx.grant_roles.get("nsfw")
        nsfw_role_id = nsfw_cfg["role_id"] if nsfw_cfg else 0
        nsfw_role = guild.get_role(nsfw_role_id) if nsfw_role_id else None
        candidates = [
            m
            for m in guild.members
            if not m.bot and (nsfw_role is None or nsfw_role not in m.roles)
        ]
        if not candidates:
            await interaction.followup.send(
                "No members found without spicy access.", ephemeral=True
            )
            return

        candidate_ids = [m.id for m in candidates]
        nsfw_role_name = nsfw_role.name if nsfw_role else ""

        def _query_promotion():
            with ctx.open_db() as conn:
                levels: dict[int, tuple[int, float]] = {}
                batch_size = 800
                for i in range(0, len(candidate_ids), batch_size):
                    batch = candidate_ids[i : i + batch_size]
                    placeholders = ", ".join("?" for _ in batch)
                    rows = conn.execute(
                        f"SELECT user_id, level, total_xp FROM member_xp "
                        f"WHERE guild_id = ? AND user_id IN ({placeholders})",
                        [guild.id, *batch],
                    ).fetchall()
                    for row in rows:
                        levels[int(row["user_id"])] = (
                            int(row["level"]),
                            float(row["total_xp"]),
                        )

                eligible_ids = [
                    m.id for m in candidates if levels.get(m.id, (1, 0))[0] > 5
                ]
                if not eligible_ids:
                    return levels, {}, {}

                pruned_users: dict[int, float] = {}
                if nsfw_role_name:
                    for i in range(0, len(eligible_ids), batch_size):
                        batch = eligible_ids[i : i + batch_size]
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

                activities = ctx.get_member_last_activity_map(
                    conn, guild.id, eligible_ids
                )
                return levels, pruned_users, activities

        levels, pruned_users, activities = await asyncio.to_thread(_query_promotion)

        eligible = [m for m in candidates if levels.get(m.id, (1, 0))[0] > 5]
        if not eligible:
            await interaction.followup.send(
                "No members above level 5 without spicy access.", ephemeral=True
            )
            return

        eligible.sort(
            key=lambda m: (-levels.get(m.id, (1, 0))[0], -levels.get(m.id, (1, 0))[1])
        )

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
    # /quality_leave commands
    # ------------------------------------------------------------------

    @quality_leave.command(name="add", description="Put a member on leave of absence.")
    @app_commands.describe(
        member="Member to put on leave.",
        days="Duration of leave in days (30, 60, or 90).",
    )
    @app_commands.choices(
        days=[
            app_commands.Choice(name="30 days", value=30),
            app_commands.Choice(name="60 days", value=60),
            app_commands.Choice(name="90 days", value=90),
        ]
    )
    async def leave_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Choice[int],
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        from services.member_quality_score import add_leave

        guild_id = interaction.guild.id if interaction.guild else ctx.guild_id
        now_ts = discord.utils.utcnow().timestamp()
        end_ts = now_ts + days.value * 86400
        with ctx.open_db() as conn:
            add_leave(conn, guild_id, member.id, now_ts, end_ts)

        await interaction.response.send_message(
            f"{member.mention} placed on leave of absence for {days.value} days.",
            ephemeral=True,
        )

    @quality_leave.command(
        name="remove", description="Remove a member's leave of absence."
    )
    @app_commands.describe(member="Member to remove from leave.")
    async def leave_remove(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        from services.member_quality_score import remove_leave

        guild_id = interaction.guild.id if interaction.guild else ctx.guild_id
        with ctx.open_db() as conn:
            removed = remove_leave(conn, guild_id, member.id)

        if removed:
            await interaction.response.send_message(
                f"{member.mention} removed from leave of absence.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{member.mention} was not on leave.", ephemeral=True
            )

    @quality_leave.command(
        name="list", description="List all members on leave of absence."
    )
    async def leave_list(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
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

        from services.member_quality_score import get_leaves

        with ctx.open_db() as conn:
            leaves = get_leaves(conn, guild.id)

        if not leaves:
            await interaction.response.send_message(
                "No members on leave.", ephemeral=True
            )
            return

        now_ts = discord.utils.utcnow().timestamp()
        lines = ["**Members on Leave of Absence**\n"]
        for uid, (_start_ts, end_ts) in leaves.items():
            m = guild.get_member(uid)
            name = m.display_name if m else f"User {uid}"
            remaining = max(0, int((end_ts - now_ts) / 86400))
            if end_ts < now_ts:
                lines.append(f"  {name} — **expired** (ended <t:{int(end_ts)}:R>)")
            else:
                lines.append(
                    f"  {name} — {remaining}d remaining (ends <t:{int(end_ts)}:R>)"
                )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(ReportsCog(bot, bot.ctx))
