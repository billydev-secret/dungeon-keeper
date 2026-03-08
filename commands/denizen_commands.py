"""Denizen role management commands."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from utils import format_user_for_log, get_bot_member

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.denizen")


def register_denizen_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(name="grant_denizen", description="Grant the Denizen role to a member.")
    @app_commands.describe(member="Member to receive the Denizen role.")
    async def grant_denizen(interaction: discord.Interaction, member: discord.Member):
        if not ctx.can_grant_denizen(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        actor = ctx.get_interaction_member(interaction)
        if member.bot:
            await interaction.response.send_message("Bots can't receive the Denizen role.", ephemeral=True)
            return

        if actor is not None and member.id == actor.id and not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You can't grant the Denizen role to yourself.", ephemeral=True
            )
            return

        if ctx.denizen_role_id <= 0:
            await interaction.response.send_message("The Denizen role is not configured yet.", ephemeral=True)
            return

        denizen_role = guild.get_role(ctx.denizen_role_id)
        if denizen_role is None:
            await interaction.response.send_message(
                "The configured Denizen role no longer exists.", ephemeral=True
            )
            return

        if denizen_role in member.roles:
            await interaction.response.send_message(
                f"{member.mention} already has {denizen_role.mention}.", ephemeral=True
            )
            return

        bot_member = get_bot_member(guild)
        if bot_member is None:
            await interaction.response.send_message(
                "Bot member context is unavailable right now.", ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the Manage Roles permission to do that.", ephemeral=True
            )
            return

        if denizen_role >= bot_member.top_role:
            await interaction.response.send_message(
                f"I can't grant {denizen_role.mention} because it is above my highest role.",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(denizen_role, reason=f"Granted by {interaction.user} via /grant_denizen")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I couldn't grant {denizen_role.mention}. Check my role hierarchy and permissions.",
                ephemeral=True,
            )
            return

        log.info(
            "%s granted %s to %s.",
            format_user_for_log(actor, interaction.user.id),
            denizen_role.name,
            format_user_for_log(member),
        )
        await interaction.response.send_message(
            f"{member.mention} has been granted {denizen_role.mention}.",
            ephemeral=False,
        )

    @bot.tree.command(name="set_greeter_role", description="Set the role allowed to run /grant_denizen.")
    @app_commands.describe(role="Role allowed to grant Denizen.")
    async def set_greeter_role(interaction: discord.Interaction, role: discord.Role):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        ctx.greeter_role_id = int(ctx.set_config_value("greeter_role_id", str(role.id)))
        await interaction.response.send_message(
            f"Members with {role.mention} can now use /grant_denizen.", ephemeral=True
        )

    @bot.tree.command(name="set_denizen_role", description="Set the role that /grant_denizen assigns.")
    @app_commands.describe(role="Role to grant with /grant_denizen.")
    async def set_denizen_role(interaction: discord.Interaction, role: discord.Role):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        ctx.denizen_role_id = int(ctx.set_config_value("denizen_role_id", str(role.id)))
        await interaction.response.send_message(
            f"/grant_denizen will now assign {role.mention}.", ephemeral=True
        )
