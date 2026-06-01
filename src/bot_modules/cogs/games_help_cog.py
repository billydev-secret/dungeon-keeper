import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games_help.embeds import build_help_embed, build_support_embed

log = logging.getLogger(__name__)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="games-help", description="List all game modes and how to use them.")
    async def help_command(self, interaction: discord.Interaction):
        log.info("%s used /games-help in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = build_help_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="games-support", description="Get a link to the support Discord server.")
    async def support_command(self, interaction: discord.Interaction):
        log.info("%s used /games-support in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = build_support_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
