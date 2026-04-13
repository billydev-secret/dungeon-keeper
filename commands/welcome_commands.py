"""Welcome and leave message preview commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from services.welcome_service import (
    build_leave_embed,
    build_welcome_embed,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_welcome_commands(bot: Bot, ctx: AppContext) -> None:

    @bot.tree.command(
        name="welcome_preview",
        description="See what the welcome message looks like using your profile.",
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
            else "No welcome channel set — use `/config welcome` first."
        )
        if ctx.welcome_ping_role_id > 0:
            channel_note += f"  Pings <@&{ctx.welcome_ping_role_id}>."
        embed = build_welcome_embed(member, ctx.welcome_message)
        await interaction.response.send_message(
            channel_note, embed=embed, ephemeral=True
        )

    @bot.tree.command(
        name="leave_preview",
        description="See what the leave message looks like using your profile.",
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
            else "No leave channel set — use `/config welcome` first."
        )
        embed = build_leave_embed(member, ctx.leave_message)
        await interaction.response.send_message(
            channel_note, embed=embed, ephemeral=True
        )
