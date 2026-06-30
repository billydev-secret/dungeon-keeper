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
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    force_end_active_game,
    get_active_game,
)

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

    async def _teardown_active_game(self, row, channel) -> None:
        """Force-close *row*'s game and tidy its message. Shared by /games end
        and /games config game-end so both stop loop games cleanly."""
        game_id = row["game_id"]
        # AMA tracks a second view + posted messages and reads them back out of
        # active_views during cleanup, so let it run BEFORE force_end_active_game
        # pops those entries. cleanup_ended_game doesn't archive, so the
        # force_end call below still records the game to history.
        if row["game_type"] == "ama":
            ama_cog = self.bot.get_cog("AMACog")
            if ama_cog and hasattr(ama_cog, "cleanup_ended_game"):
                await ama_cog.cleanup_ended_game(
                    row["channel_id"], game_id, channel=channel,
                )
        await force_end_active_game(self.bot, self.db, game_id)
        # Grey out the live game message's buttons if we can find it.
        if channel and row["message_id"]:
            try:
                msg = await channel.fetch_message(row["message_id"])
                await msg.edit(view=None)
            except Exception:
                pass

    @app_commands.command(name="end", description="End the active game in this channel (host or mod).")
    async def games_end(self, interaction: discord.Interaction):
        log.info("%s used /games end in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.response.send_message(
                "There's no active game in this channel.", ephemeral=True,
            )
            return

        is_host = interaction.user.id == row["host_id"]
        is_mod = bool(interaction.guild) and has_mod_or_admin_permissions(
            interaction.user.guild_permissions
        )
        if not (is_host or is_mod):
            await interaction.response.send_message(
                "Only the game's host or a moderator can end it.", ephemeral=True,
            )
            return

        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            await self._teardown_active_game(row, channel)
            try:
                await channel.send(embed=build_force_end_embed(row["game_type"]))
            except Exception:
                pass

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=view, ephemeral=True,
        )

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
        await interaction.response.defer(ephemeral=True)
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.followup.send(
                "No active game in this channel.", ephemeral=True
            )
            return
        await self._teardown_active_game(row, interaction.channel)
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
    bot.tree.remove_command("end")
    games.add_command(cog.config_group)
    games.add_command(cog.games_end)
    bot.tree.add_command(games)
