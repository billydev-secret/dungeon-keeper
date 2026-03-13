"""Welcome and leave message configuration commands."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from services.welcome_service import (
    build_leave_embed,
    build_welcome_embed,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.welcome")

_PLACEHOLDER_SHORT = "Placeholders: {member} {member_name} {member_id} {server} {member_count}"


class _WelcomeMessageModal(discord.ui.Modal, title="Set Welcome Message"):
    message: discord.ui.TextInput = discord.ui.TextInput(
        label="Message template",
        placeholder=_PLACEHOLDER_SHORT,
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, ctx: AppContext, current: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.message.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.ctx.welcome_message = self.ctx.set_config_value("welcome_message", self.message.value)
        await interaction.response.send_message(
            "Welcome message updated. Use `/welcome_preview` to see how it looks.", ephemeral=True
        )


class _LeaveMessageModal(discord.ui.Modal, title="Set Leave Message"):
    message: discord.ui.TextInput = discord.ui.TextInput(
        label="Message template",
        placeholder=_PLACEHOLDER_SHORT,
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, ctx: AppContext, current: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.message.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.ctx.leave_message = self.ctx.set_config_value("leave_message", self.message.value)
        await interaction.response.send_message(
            "Leave message updated. Use `/leave_preview` to see how it looks.", ephemeral=True
        )


def register_welcome_commands(bot: "Bot", ctx: "AppContext") -> None:

    # -------------------------------------------------------------------------
    # Welcome commands
    # -------------------------------------------------------------------------

    @bot.tree.command(
        name="welcome_set_here",
        description="Send welcome messages in this channel when a member joins.",
    )
    async def welcome_set_here(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        ctx.welcome_channel_id = int(ctx.set_config_value("welcome_channel_id", str(channel.id)))
        await interaction.response.send_message(
            f"Welcome messages will be posted in {channel.mention}.", ephemeral=True
        )

    @bot.tree.command(name="welcome_disable", description="Disable welcome messages.")
    async def welcome_disable(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        ctx.welcome_channel_id = int(ctx.set_config_value("welcome_channel_id", "0"))
        await interaction.response.send_message("Welcome messages disabled.", ephemeral=True)

    @bot.tree.command(
        name="welcome_set_message",
        description="Set the welcome message template.",
    )
    async def welcome_set_message(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        await interaction.response.send_modal(_WelcomeMessageModal(ctx, ctx.welcome_message))

    @bot.tree.command(
        name="welcome_preview",
        description="Preview the welcome message using your own profile.",
    )
    async def welcome_preview(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        member = ctx.get_interaction_member(interaction)
        if member is None:
            await interaction.response.send_message(
                "Could not resolve your member record.", ephemeral=True
            )
            return

        channel_note = (
            f"Would post in <#{ctx.welcome_channel_id}>."
            if ctx.welcome_channel_id > 0
            else "No welcome channel set — use `/welcome_set_here` first."
        )
        embed = build_welcome_embed(member, ctx.welcome_message)
        await interaction.response.send_message(channel_note, embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # Leave commands
    # -------------------------------------------------------------------------

    @bot.tree.command(
        name="leave_set_here",
        description="Send leave messages in this channel when a member departs.",
    )
    async def leave_set_here(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        ctx.leave_channel_id = int(ctx.set_config_value("leave_channel_id", str(channel.id)))
        await interaction.response.send_message(
            f"Leave messages will be posted in {channel.mention}.", ephemeral=True
        )

    @bot.tree.command(name="leave_disable", description="Disable leave messages.")
    async def leave_disable(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        ctx.leave_channel_id = int(ctx.set_config_value("leave_channel_id", "0"))
        await interaction.response.send_message("Leave messages disabled.", ephemeral=True)

    @bot.tree.command(
        name="leave_set_message",
        description="Set the leave message template.",
    )
    async def leave_set_message(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        await interaction.response.send_modal(_LeaveMessageModal(ctx, ctx.leave_message))

    @bot.tree.command(
        name="leave_preview",
        description="Preview the leave message using your own profile.",
    )
    async def leave_preview(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        member = ctx.get_interaction_member(interaction)
        if member is None:
            await interaction.response.send_message(
                "Could not resolve your member record.", ephemeral=True
            )
            return

        channel_note = (
            f"Would post in <#{ctx.leave_channel_id}>."
            if ctx.leave_channel_id > 0
            else "No leave channel set — use `/leave_set_here` first."
        )
        embed = build_leave_embed(member, ctx.leave_message)
        await interaction.response.send_message(channel_note, embed=embed, ephemeral=True)
