"""Quality-leave commands.

Member activity/role/engagement reports were migrated to the web dashboard
(`src/web_server/routes/reports.py`); only the quality-leave roster management
remains as a slash command.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


class ReportsCog(commands.Cog):
    quality_leave = app_commands.Group(
        name="quality_leave",
        description="Pause quality scoring for members on leave of absence.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

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

        from bot_modules.services.member_quality_score import add_leave

        guild_id = interaction.guild.id if interaction.guild else ctx.guild_id
        now_ts = discord.utils.utcnow().timestamp()
        end_ts = now_ts + days.value * 86400
        def _do_add_leave():
            with ctx.open_db() as conn:
                add_leave(conn, guild_id, member.id, now_ts, end_ts)

        await asyncio.to_thread(_do_add_leave)

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

        from bot_modules.services.member_quality_score import remove_leave

        guild_id = interaction.guild.id if interaction.guild else ctx.guild_id
        def _do_remove_leave():
            with ctx.open_db() as conn:
                return remove_leave(conn, guild_id, member.id)

        removed = await asyncio.to_thread(_do_remove_leave)

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

        from bot_modules.services.member_quality_score import get_leaves

        gid = guild.id

        def _do_get_leaves():
            with ctx.open_db() as conn:
                return get_leaves(conn, gid)

        leaves = await asyncio.to_thread(_do_get_leaves)

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
