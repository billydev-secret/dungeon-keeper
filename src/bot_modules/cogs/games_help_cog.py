import logging

import discord

from bot_modules.games.command_groups import games
from bot_modules.core.branding import resolve_accent_color
from bot_modules.games_help.embeds import build_help_embed, build_support_embed
from bot_modules.games.utils.game_manager import channel_name

log = logging.getLogger(__name__)


@games.command(name="help", description="List all game modes and how to use them.")
async def help_command(interaction: discord.Interaction):
    log.info("%s used /games help in #%s", interaction.user.display_name, channel_name(interaction.channel))
    guild = interaction.guild
    colour = await resolve_accent_color(interaction.client.ctx.db_path, guild) if guild else None
    embed = build_help_embed(colour=colour)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@games.command(name="support", description="Get a link to the support Discord server.")
async def support_command(interaction: discord.Interaction):
    log.info("%s used /games support in #%s", interaction.user.display_name, channel_name(interaction.channel))
    guild = interaction.guild
    colour = await resolve_accent_color(interaction.client.ctx.db_path, guild) if guild else None
    embed = build_support_embed(colour=colour)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    pass
