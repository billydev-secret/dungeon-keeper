"""Spoiler guard management commands."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from utils import get_guild_channel_or_thread

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_spoiler_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="spoiler_guard_add_here",
        description="Enable spoiler guard in this channel or thread.",
    )
    async def spoiler_guard_add_here(interaction: discord.Interaction):
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

        ctx.spoiler_required_channels = ctx.add_config_id_value("spoiler_required_channels", channel.id)
        await interaction.response.send_message(
            f"Spoiler guard enabled for {channel.mention}. "
            f"Guarded channel IDs: {sorted(ctx.spoiler_required_channels)}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="spoiler_guard_remove_here",
        description="Disable spoiler guard in this channel or thread.",
    )
    async def spoiler_guard_remove_here(interaction: discord.Interaction):
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

        ctx.spoiler_required_channels = ctx.remove_config_id_value("spoiler_required_channels", channel.id)
        await interaction.response.send_message(
            f"Spoiler guard disabled for {channel.mention}. "
            f"Guarded channel IDs: {sorted(ctx.spoiler_required_channels)}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="spoiler_guarded_channels",
        description="List channels and threads where spoiler guard is enabled.",
    )
    async def spoiler_guarded_channels(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ctx.spoiler_required_channels:
            await interaction.response.send_message(
                "Spoiler guard is currently disabled in all channels.", ephemeral=True
            )
            return

        guild = interaction.guild
        labels = []
        for channel_id in sorted(ctx.spoiler_required_channels):
            channel = get_guild_channel_or_thread(guild, channel_id) if guild else None
            labels.append(channel.mention if channel else f"`{channel_id}`")

        await interaction.response.send_message(
            "Spoiler guard enabled in: " + ", ".join(labels), ephemeral=True
        )
