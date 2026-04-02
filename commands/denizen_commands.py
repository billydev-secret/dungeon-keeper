"""Role grant commands — single /grant command driven from the grant_roles DB table."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from utils import format_user_for_log, get_bot_member

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.denizen")


def _resolve_grant_message(
    template: str,
    member: discord.Member,
    role: discord.Role,
    actor: discord.Member | None,
    interaction: discord.Interaction,
) -> str:
    return (
        template
        .replace("{member}", member.mention)
        .replace("{member_name}", member.display_name)
        .replace("{role}", role.mention)
        .replace("{role_name}", role.name)
        .replace("{actor}", actor.mention if actor else interaction.user.mention)
    )


async def _execute_grant(
    interaction: discord.Interaction,
    member: discord.Member,
    role_id: int,
    log_channel_id: int,
    announce_channel_id: int,
    grant_message: str,
    ctx: "AppContext",
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

    # Defer before the slow add_roles API call to avoid the 3-second timeout.
    await interaction.response.defer()

    try:
        await member.add_roles(role, reason=f"Granted by {interaction.user} via slash command")
    except discord.Forbidden:
        await interaction.followup.send(
            f"I couldn't grant {role.mention}. Check my role hierarchy and permissions.", ephemeral=True
        )
        return

    from xp_system import log_role_event
    with ctx.open_db() as db_conn:
        log_role_event(db_conn, guild.id, member.id, role.name, "grant")

    log.info(
        "%s granted %s to %s.",
        format_user_for_log(actor, interaction.user.id),
        role.name,
        format_user_for_log(member),
    )
    await interaction.followup.send(
        f"{member.mention} has been granted {role.mention}."
    )

    if announce_channel_id > 0 and grant_message:
        announce_channel = guild.get_channel(announce_channel_id)
        if isinstance(announce_channel, discord.TextChannel):
            await announce_channel.send(
                _resolve_grant_message(grant_message, member, role, actor, interaction)
            )

    if log_channel_id > 0:
        log_channel = guild.get_channel(log_channel_id)
        if isinstance(log_channel, discord.TextChannel):
            await log_channel.send(
                f"{member.mention} was granted {role.mention} by {interaction.user.mention}."
            )


_GRANT_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in [
        ("denizen", "Denizen"), ("nsfw", "NSFW"), ("veteran", "Veteran"),
        ("kink", "Kink"), ("goldengirl", "Golden Girl"),
    ]
]


def register_denizen_commands(bot: "Bot", ctx: "AppContext") -> None:

    @bot.tree.command(name="grant", description="Grant a role to a member.")
    @app_commands.describe(
        role="Which role to grant.",
        member="Member to receive the role.",
    )
    @app_commands.choices(role=_GRANT_CHOICES)
    async def grant_cmd(interaction: discord.Interaction, role: str, member: discord.Member) -> None:
        if not ctx.can_use_grant_role(interaction, role):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        cfg = ctx.grant_roles.get(role)
        if cfg is None:
            await interaction.response.send_message("This grant role is not configured.", ephemeral=True)
            return
        await _execute_grant(
            interaction, member,
            role_id=cfg["role_id"],
            log_channel_id=cfg["log_channel_id"],
            announce_channel_id=cfg["announce_channel_id"],
            grant_message=cfg["grant_message"],
            ctx=ctx,
        )
