"""Inactivity prune commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.inactivity_prune_service import (
    add_prune_exception,
    get_prune_exception_ids,
    get_prune_rule,
    remove_prune_exception,
    run_prune_for_guild,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


class InactivityPruneCog(commands.Cog):
    inactivity_prune = app_commands.Group(
        name="inactivity_prune",
        description="Automatically remove a role from members who go inactive.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @inactivity_prune.command(
        name="status",
        description="Show the current prune rule, threshold, and exemption list.",
    )
    async def prune_status(self, interaction: discord.Interaction) -> None:
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

        rule = get_prune_rule(ctx.db_path, guild.id)
        if rule is None:
            await interaction.response.send_message(
                "No inactivity prune rule is configured.", ephemeral=True
            )
            return

        role = guild.get_role(int(rule["role_id"]))
        role_label = f"@{role.name}" if role else f"<deleted role {rule['role_id']}>"
        days = int(rule["inactivity_days"])

        exception_ids = get_prune_exception_ids(ctx.db_path, guild.id)
        exception_mentions = []
        for uid in sorted(exception_ids):
            member = guild.get_member(uid)
            exception_mentions.append(
                member.display_name if member else f"<user {uid}>"
            )

        exempt_block = ", ".join(exception_mentions) if exception_mentions else "none"
        await interaction.response.send_message(
            f"**Inactivity Prune Config**\n"
            f"Role: **{role_label}**\n"
            f"Inactivity threshold: **{days} day(s)**\n"
            f"Schedule: daily at midnight UTC\n"
            f"Exemptions: {exempt_block}",
            ephemeral=True,
        )

    @inactivity_prune.command(
        name="exempt",
        description="Protect a member from being pruned.",
    )
    @app_commands.describe(member="Member to exempt.")
    async def prune_exempt(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
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

        add_prune_exception(ctx.db_path, guild.id, member.id)
        await interaction.response.send_message(
            f"**{member.display_name}** is now exempt from inactivity pruning.",
            ephemeral=True,
        )

    @inactivity_prune.command(
        name="unexempt",
        description="Remove a member's prune exemption.",
    )
    @app_commands.describe(member="Member to remove from the exemption list.")
    async def prune_unexempt(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
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

        removed = remove_prune_exception(ctx.db_path, guild.id, member.id)
        if removed:
            await interaction.response.send_message(
                f"**{member.display_name}** is no longer exempt from inactivity pruning.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"**{member.display_name}** was not on the exemption list.",
                ephemeral=True,
            )

    @inactivity_prune.command(
        name="run",
        description="Trigger a prune run right now instead of waiting for the daily schedule.",
    )
    async def prune_run(self, interaction: discord.Interaction) -> None:
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

        rule = get_prune_rule(ctx.db_path, guild.id)
        if rule is None:
            await interaction.response.send_message(
                "No inactivity prune rule is configured.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await run_prune_for_guild(
            ctx.bot,
            ctx.db_path,
            guild.id,
            int(rule["role_id"]),
            int(rule["inactivity_days"]),
        )
        await interaction.followup.send("Inactivity prune completed.", ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(InactivityPruneCog(bot, bot.ctx))
