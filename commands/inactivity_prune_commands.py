"""Inactivity prune slash commands.

Commands (all mod-only, under the /inactivity_prune group):
  /inactivity_prune status   — show current config and exemption list
  /inactivity_prune exempt   — exempt a member from pruning
  /inactivity_prune unexempt — remove a member's exemption
  /inactivity_prune run      — trigger an immediate prune run
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.inactivity_prune_service import (
    add_prune_exception,
    get_prune_exception_ids,
    get_prune_rule,
    remove_prune_exception,
    run_prune_for_guild,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_inactivity_prune_commands(bot: Bot, ctx: AppContext) -> None:
    prune_group = app_commands.Group(
        name="inactivity_prune",
        description="Manage the inactivity prune schedule, exemptions, and status.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @prune_group.command(
        name="status",
        description="Show the current inactivity prune configuration.",
    )
    async def prune_status(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        rule = get_prune_rule(ctx.db_path, guild.id)
        if rule is None:
            await interaction.response.send_message("No inactivity prune rule is configured.", ephemeral=True)
            return

        role = guild.get_role(int(rule["role_id"]))
        role_label = f"@{role.name}" if role else f"<deleted role {rule['role_id']}>"
        days = int(rule["inactivity_days"])

        exception_ids = get_prune_exception_ids(ctx.db_path, guild.id)
        exception_mentions = []
        for uid in sorted(exception_ids):
            member = guild.get_member(uid)
            exception_mentions.append(member.display_name if member else f"<user {uid}>")

        exempt_block = ", ".join(exception_mentions) if exception_mentions else "none"
        await interaction.response.send_message(
            f"**Inactivity Prune Config**\n"
            f"Role: **{role_label}**\n"
            f"Inactivity threshold: **{days} day(s)**\n"
            f"Schedule: daily at midnight UTC\n"
            f"Exemptions: {exempt_block}",
            ephemeral=True,
        )

    @prune_group.command(
        name="exempt",
        description="Exempt a member from inactivity pruning.",
    )
    @app_commands.describe(member="Member to exempt.")
    async def prune_exempt(
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        add_prune_exception(ctx.db_path, guild.id, member.id)
        await interaction.response.send_message(
            f"**{member.display_name}** is now exempt from inactivity pruning.", ephemeral=True
        )

    @prune_group.command(
        name="unexempt",
        description="Remove a member's inactivity prune exemption.",
    )
    @app_commands.describe(member="Member to remove from the exemption list.")
    async def prune_unexempt(
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        removed = remove_prune_exception(ctx.db_path, guild.id, member.id)
        if removed:
            await interaction.response.send_message(
                f"**{member.display_name}** is no longer exempt from inactivity pruning.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"**{member.display_name}** was not on the exemption list.", ephemeral=True
            )

    @prune_group.command(
        name="run",
        description="Run the inactivity prune immediately.",
    )
    async def prune_run(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        rule = get_prune_rule(ctx.db_path, guild.id)
        if rule is None:
            await interaction.response.send_message("No inactivity prune rule is configured.", ephemeral=True)
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

    bot.tree.add_command(prune_group)
