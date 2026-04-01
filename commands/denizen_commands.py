"""Role grant commands — data-driven from the grant_roles DB table."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from db_utils import (
    add_grant_permission,
    get_grant_permissions,
    remove_grant_permission,
)
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

    try:
        await member.add_roles(role, reason=f"Granted by {interaction.user} via slash command")
    except discord.Forbidden:
        await interaction.response.send_message(
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
    await interaction.response.send_message(
        f"{member.mention} has been granted {role.mention}.", ephemeral=False
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


def _make_grant_command(bot: "Bot", ctx: "AppContext", *, grant_name: str) -> None:
    """Register /grant_<name> for one role type, reading config from ctx.grant_roles at runtime."""

    @bot.tree.command(name=f"grant_{grant_name}", description=f"Grant the {grant_name} role to a member.")
    @app_commands.describe(member=f"Member to receive the {grant_name} role.")
    async def grant_cmd(interaction: discord.Interaction, member: discord.Member) -> None:
        if not ctx.can_use_grant_role(interaction, grant_name):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        cfg = ctx.grant_roles.get(grant_name)
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


def _parse_mention(text: str) -> tuple[str, int] | None:
    """Parse a role/user mention or raw ID. Returns (entity_type, entity_id) or None."""
    import re
    text = text.strip()
    # <@!123> or <@123>
    m = re.match(r"<@!?(\d+)>", text)
    if m:
        return ("user", int(m.group(1)))
    # <@&123>
    m = re.match(r"<@&(\d+)>", text)
    if m:
        return ("role", int(m.group(1)))
    # raw ID — guess based on guild
    if text.isdigit():
        return ("unknown", int(text))
    return None


def register_denizen_commands(bot: "Bot", ctx: "AppContext") -> None:
    for name in ("denizen", "nsfw", "veteran", "kink", "goldengirl"):
        _make_grant_command(bot, ctx, grant_name=name)

    # --- Permission management commands ---

    _grant_name_choices = [
        app_commands.Choice(name=label, value=key)
        for key, label in [
            ("denizen", "Denizen"), ("nsfw", "NSFW"), ("veteran", "Veteran"),
            ("kink", "Kink"), ("goldengirl", "Golden Girl"),
        ]
    ]

    @bot.tree.command(
        name="grant_allow",
        description="Allow a user or role to use a grant command.",
    )
    @app_commands.describe(
        grant="Which grant command to configure.",
        allowed="The user or role to allow (mention or ID).",
    )
    @app_commands.choices(grant=_grant_name_choices)
    async def grant_allow(interaction: discord.Interaction, grant: str, allowed: str) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        parsed = _parse_mention(allowed)
        if parsed is None:
            await interaction.response.send_message(f"Could not parse `{allowed}` as a user or role.", ephemeral=True)
            return

        entity_type, entity_id = parsed
        if entity_type == "unknown":
            # Try to resolve: role first, then user
            if guild.get_role(entity_id):
                entity_type = "role"
            else:
                entity_type = "user"

        with ctx.open_db() as conn:
            added = add_grant_permission(conn, guild.id, grant, entity_type, entity_id)

        if entity_type == "role":
            label = f"<@&{entity_id}>"
        else:
            label = f"<@{entity_id}>"

        if added:
            await interaction.response.send_message(
                f"{label} can now use `/grant_{grant}`.", ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{label} already has permission for `/grant_{grant}`.", ephemeral=True,
            )

    @bot.tree.command(
        name="grant_deny",
        description="Remove a user or role's permission for a grant command.",
    )
    @app_commands.describe(
        grant="Which grant command to configure.",
        denied="The user or role to remove (mention or ID).",
    )
    @app_commands.choices(grant=_grant_name_choices)
    async def grant_deny(interaction: discord.Interaction, grant: str, denied: str) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        parsed = _parse_mention(denied)
        if parsed is None:
            await interaction.response.send_message(f"Could not parse `{denied}` as a user or role.", ephemeral=True)
            return

        entity_type, entity_id = parsed
        if entity_type == "unknown":
            if guild.get_role(entity_id):
                entity_type = "role"
            else:
                entity_type = "user"

        with ctx.open_db() as conn:
            removed = remove_grant_permission(conn, guild.id, grant, entity_type, entity_id)

        if entity_type == "role":
            label = f"<@&{entity_id}>"
        else:
            label = f"<@{entity_id}>"

        if removed:
            await interaction.response.send_message(
                f"{label} can no longer use `/grant_{grant}`.", ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{label} didn't have permission for `/grant_{grant}`.", ephemeral=True,
            )

    @bot.tree.command(
        name="grant_permissions",
        description="List who can use each grant command.",
    )
    async def grant_permissions_cmd(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        lines: list[str] = []
        with ctx.open_db() as conn:
            for grant_name, cfg in ctx.grant_roles.items():
                perms = get_grant_permissions(conn, guild.id, grant_name)
                if perms:
                    entries = []
                    for etype, eid in perms:
                        if etype == "role":
                            entries.append(f"<@&{eid}>")
                        else:
                            entries.append(f"<@{eid}>")
                    lines.append(f"**{cfg['label']}**: {', '.join(entries)}")
                else:
                    lines.append(f"**{cfg['label']}**: mod-only")

        await interaction.response.send_message(
            "**Grant Permissions**\n" + "\n".join(lines) + "\n\n*Mods always have access.*",
            ephemeral=True,
        )
