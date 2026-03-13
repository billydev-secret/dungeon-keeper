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


async def _execute_grant(
    interaction: discord.Interaction,
    member: discord.Member,
    role_id: int,
    log_channel_id: int,
    ctx: AppContext,
) -> None:
    """Shared grant logic for all role-grant commands."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    actor = ctx.get_interaction_member(interaction)

    if member.bot:
        await interaction.response.send_message("Bots can't receive this role.", ephemeral=True)
        return

    if actor is not None and member.id == actor.id and not ctx.is_mod(interaction):
        await interaction.response.send_message("You can't grant this role to yourself.", ephemeral=True)
        return

    if role_id <= 0:
        await interaction.response.send_message("This role is not configured yet.", ephemeral=True)
        return

    role = guild.get_role(role_id)
    if role is None:
        await interaction.response.send_message("The configured role no longer exists.", ephemeral=True)
        return

    if role in member.roles:
        await interaction.response.send_message(
            f"{member.mention} already has {role.mention}.", ephemeral=True
        )
        return

    bot_member = get_bot_member(guild)
    if bot_member is None:
        await interaction.response.send_message("Bot member context is unavailable right now.", ephemeral=True)
        return

    if not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "I need the Manage Roles permission to do that.", ephemeral=True
        )
        return

    if role >= bot_member.top_role:
        await interaction.response.send_message(
            f"I can't grant {role.mention} because it is above my highest role.", ephemeral=True
        )
        return

    try:
        await member.add_roles(role, reason=f"Granted by {interaction.user} via slash command")
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I couldn't grant {role.mention}. Check my role hierarchy and permissions.", ephemeral=True
        )
        return

    log.info(
        "%s granted %s to %s.",
        format_user_for_log(actor, interaction.user.id),
        role.name,
        format_user_for_log(member),
    )
    await interaction.response.send_message(
        f"{member.mention} has been granted {role.mention}.", ephemeral=False
    )

    if log_channel_id > 0:
        log_channel = guild.get_channel(log_channel_id)
        if isinstance(log_channel, discord.TextChannel):
            await log_channel.send(
                f"{member.mention} was granted {role.mention} by {interaction.user.mention}."
            )


def _make_set_role_commands(
    bot: Bot,
    ctx: AppContext,
    *,
    grant_name: str,
    role_attr: str,
    log_attr: str,
    role_config_key: str,
    log_config_key: str,
    can_grant,
) -> None:
    """Register /grant_X, /set_X_role, /set_X_log_here, /X_log_disable for one role type."""

    @bot.tree.command(name=f"grant_{grant_name}", description=f"Grant the {grant_name} role to a member.")
    @app_commands.describe(member=f"Member to receive the {grant_name} role.")
    async def grant_cmd(interaction: discord.Interaction, member: discord.Member):
        if not can_grant(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        await _execute_grant(
            interaction, member,
            role_id=getattr(ctx, role_attr),
            log_channel_id=getattr(ctx, log_attr),
            ctx=ctx,
        )

    @bot.tree.command(name=f"set_{grant_name}_role", description=f"Set the role that /grant_{grant_name} assigns.")
    @app_commands.describe(role=f"Role to grant with /grant_{grant_name}.")
    async def set_role_cmd(interaction: discord.Interaction, role: discord.Role):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        setattr(ctx, role_attr, int(ctx.set_config_value(role_config_key, str(role.id))))
        await interaction.response.send_message(
            f"/grant_{grant_name} will now assign {role.mention}.", ephemeral=True
        )

    @bot.tree.command(name=f"set_{grant_name}_log_here", description=f"Log /grant_{grant_name} grants in this channel.")
    async def set_log_cmd(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        setattr(ctx, log_attr, interaction.channel_id)
        ctx.set_config_value(log_config_key, str(interaction.channel_id))
        await interaction.response.send_message(
            f"/grant_{grant_name} grants will now be logged in this channel.", ephemeral=True
        )

    @bot.tree.command(name=f"{grant_name}_log_disable", description=f"Stop logging /grant_{grant_name} grants.")
    async def disable_log_cmd(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        setattr(ctx, log_attr, 0)
        ctx.set_config_value(log_config_key, "0")
        await interaction.response.send_message(
            f"/grant_{grant_name} logging disabled.", ephemeral=True
        )


def register_denizen_commands(bot: Bot, ctx: AppContext) -> None:
    # /set_greeter_role — controls who can use /grant_denizen
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

    _make_set_role_commands(
        bot, ctx,
        grant_name="denizen",
        role_attr="denizen_role_id",
        log_attr="denizen_log_channel_id",
        role_config_key="denizen_role_id",
        log_config_key="denizen_log_channel_id",
        can_grant=ctx.can_grant_denizen,
    )

    _make_set_role_commands(
        bot, ctx,
        grant_name="nsfw",
        role_attr="nsfw_role_id",
        log_attr="nsfw_log_channel_id",
        role_config_key="nsfw_role_id",
        log_config_key="nsfw_log_channel_id",
        can_grant=ctx.can_grant_denizen,
    )

    _make_set_role_commands(
        bot, ctx,
        grant_name="veteran",
        role_attr="veteran_role_id",
        log_attr="veteran_log_channel_id",
        role_config_key="veteran_role_id",
        log_config_key="veteran_log_channel_id",
        can_grant=ctx.can_grant_denizen,
    )
