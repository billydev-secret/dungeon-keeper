import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games_config.embeds import (
    build_force_end_embed,
    build_game_status_embed,
)
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.games.command_groups import games

log = logging.getLogger(__name__)


def is_mod_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)
    return app_commands.check(predicate)


class GamesConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    config_group = app_commands.Group(
        name="config",
        description="In-channel game management commands (mods only).",
    )

    @config_group.command(name="game-status", description="Show the active game in this channel.")
    @is_mod_or_admin()
    async def game_status(self, interaction: discord.Interaction):
        log.info("%s used /games config game-status in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        from bot_modules.games.utils.game_manager import get_active_game
        row = await get_active_game(self.db, interaction.channel_id)
        embed = build_game_status_embed(row)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="game-end", description="Force-close the active game in this channel.")
    @is_mod_or_admin()
    async def game_end(self, interaction: discord.Interaction):
        log.info("%s used /games config game-end in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        from bot_modules.games.utils.game_manager import get_active_game, end_game
        await interaction.response.defer(ephemeral=True)
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.followup.send(
                "No active game in this channel.", ephemeral=True
            )
            return
        await end_game(self.db, row["game_id"])
        if row["game_type"] == "ama":
            ama_cog = self.bot.get_cog("AMACog")
            if ama_cog and hasattr(ama_cog, "cleanup_ended_game"):
                await ama_cog.cleanup_ended_game(
                    interaction.channel_id,
                    row["game_id"],
                    channel=interaction.channel,
                )
        # Stop the view if tracked
        if row["game_id"] in self.bot.active_views:
            view = self.bot.active_views.pop(row["game_id"])
            view.stop()
        embed = build_force_end_embed(row["game_type"])
        await interaction.followup.send(embed=embed)

    @game_status.error
    @game_end.error
    async def mod_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            log.error("Permission denied for %s on mod command in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
            try:
                await interaction.response.send_message(
                    "❌ You need moderator or admin permissions to use this command.", ephemeral=True
                )
            except discord.NotFound:
                pass
        else:
            log.error("Error in config command: %s", error, exc_info=True)


async def setup(bot: commands.Bot):
    cog = GamesConfigCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("config")
    games.add_command(cog.config_group)
    bot.tree.add_command(games)
