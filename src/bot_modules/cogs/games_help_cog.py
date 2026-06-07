import logging

import discord

from bot_modules.games.command_groups import games
from bot_modules.games_help.embeds import build_help_embed, build_support_embed

log = logging.getLogger(__name__)


@games.command(name="help", description="List all game modes and how to use them.")
async def help_command(interaction: discord.Interaction):
    log.info("%s used /games help in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
    embed = build_help_embed()
    await interaction.response.send_message(embed=embed, ephemeral=True)


@games.command(name="support", description="Get a link to the support Discord server.")
async def support_command(interaction: discord.Interaction):
    log.info("%s used /games support in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
    embed = build_support_embed()
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    pass
