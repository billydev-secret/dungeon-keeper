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


_GRANT_PLACEHOLDER_HELP = (
    "`{member}` — mention  •  `{member_name}` — display name  •  "
    "`{role}` — role mention  •  `{role_name}` — role name  •  `{actor}` — granter mention"
)
_GRANT_PLACEHOLDER_SHORT = "{member} {member_name} {role} {role_name} {actor}"


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


class _GrantMessageModal(discord.ui.Modal, title="Set Grant Message"):
    message: discord.ui.TextInput = discord.ui.TextInput(
        label="Message template",
        placeholder=_GRANT_PLACEHOLDER_SHORT,
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, *, ctx: AppContext, message_attr: str, message_config_key: str, grant_name: str, current: str) -> None:  # noqa: E501
        super().__init__(title=f"Set {grant_name.title()} Grant Message")
        self._ctx = ctx
        self._message_attr = message_attr
        self._message_config_key = message_config_key
        self._grant_name = grant_name
        self.message.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        setattr(self._ctx, self._message_attr, self.message.value)
        self._ctx.set_config_value(self._message_config_key, self.message.value)
        await interaction.response.send_message(
            f"/grant_{self._grant_name} message updated.", ephemeral=True
        )


async def _execute_grant(
    interaction: discord.Interaction,
    member: discord.Member,
    role_id: int,
    log_channel_id: int,
    announce_channel_id: int,
    grant_message: str,
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


def _register_grant_message_command(
    bot: Bot,
    ctx: AppContext,
    *,
    grant_name: str,
    message_attr: str,
    message_config_key: str,
) -> None:
    @bot.tree.command(name=f"set_{grant_name}_message", description=f"Set the message posted on /grant_{grant_name}.")
    async def set_message_cmd(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        current = getattr(ctx, message_attr, "")
        await interaction.response.send_modal(_GrantMessageModal(
            ctx=ctx,
            message_attr=message_attr,
            message_config_key=message_config_key,
            grant_name=grant_name,
            current=current,
        ))


def _make_set_role_commands(
    bot: Bot,
    ctx: AppContext,
    *,
    grant_name: str,
    role_attr: str,
    log_attr: str,
    announce_attr: str,
    role_config_key: str,
    log_config_key: str,
    announce_config_key: str,
    message_attr: str | None = None,
    message_config_key: str | None = None,
    can_grant,
) -> None:
    """Register /grant_X, /set_X_role, /set_X_log_here, /X_log_disable for one role type.
    If message_attr is provided, also registers /set_X_message."""

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
            announce_channel_id=getattr(ctx, announce_attr),
            grant_message=getattr(ctx, message_attr) if message_attr else "",
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

    desc = f"Post /grant_{grant_name} messages in this channel."
    @bot.tree.command(name=f"set_{grant_name}_announce_here", description=desc)
    async def set_announce_cmd(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        setattr(ctx, announce_attr, interaction.channel_id)
        ctx.set_config_value(announce_config_key, str(interaction.channel_id))
        await interaction.response.send_message(
            f"/grant_{grant_name} messages will be posted in this channel.", ephemeral=True
        )

    @bot.tree.command(name=f"{grant_name}_announce_disable", description=f"Stop posting /grant_{grant_name} messages.")
    async def disable_announce_cmd(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        setattr(ctx, announce_attr, 0)
        ctx.set_config_value(announce_config_key, "0")
        await interaction.response.send_message(
            f"/grant_{grant_name} announce channel disabled.", ephemeral=True
        )

    if message_attr and message_config_key:
        _register_grant_message_command(
            bot, ctx,
            grant_name=grant_name,
            message_attr=message_attr,
            message_config_key=message_config_key,
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
        announce_attr="denizen_announce_channel_id",
        role_config_key="denizen_role_id",
        log_config_key="denizen_log_channel_id",
        announce_config_key="denizen_announce_channel_id",
        message_attr="denizen_grant_message",
        message_config_key="denizen_grant_message",
        can_grant=ctx.can_grant_denizen,
    )

    _make_set_role_commands(
        bot, ctx,
        grant_name="nsfw",
        role_attr="nsfw_role_id",
        log_attr="nsfw_log_channel_id",
        announce_attr="nsfw_announce_channel_id",
        role_config_key="nsfw_role_id",
        log_config_key="nsfw_log_channel_id",
        announce_config_key="nsfw_announce_channel_id",
        message_attr="nsfw_grant_message",
        message_config_key="nsfw_grant_message",
        can_grant=ctx.can_grant_denizen,
    )

    _make_set_role_commands(
        bot, ctx,
        grant_name="veteran",
        role_attr="veteran_role_id",
        log_attr="veteran_log_channel_id",
        announce_attr="veteran_announce_channel_id",
        role_config_key="veteran_role_id",
        log_config_key="veteran_log_channel_id",
        announce_config_key="veteran_announce_channel_id",
        message_attr="veteran_grant_message",
        message_config_key="veteran_grant_message",
        can_grant=ctx.can_grant_denizen,
    )
