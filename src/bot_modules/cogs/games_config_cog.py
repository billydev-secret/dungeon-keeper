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

    # Games with no fixed roster — anyone takes part by acting during an open
    # phase, so there's nothing to join or leave.
    OPEN_SUBMISSION_GAMES = {
        "hottakes", "fantasies", "ttl", "wyr", "nhie", "traditional",
    }

    async def _can_manage_others(self, interaction: discord.Interaction, host_id) -> bool:
        """True if the caller may add/remove *other* players: the game's host,
        a mod/admin, or a holder of the configured Game Host role."""
        if interaction.user.id == host_id:
            return True
        if has_mod_or_admin_permissions(getattr(interaction.user, "guild_permissions", None)):
            return True
        row = await self.db.fetchone(
            "SELECT role_id FROM games_editor_role WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        if row:
            user_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
            if int(row["role_id"]) in user_role_ids:
                return True
        return False

    async def _membership_command(self, interaction: discord.Interaction, user, joining: bool):
        verb = "join" if joining else "leave"
        log.info("%s used /games %s in #%s", interaction.user.display_name, verb, interaction.channel.name if interaction.channel else "unknown")
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.response.send_message(
                "There's no active game in this channel.", ephemeral=True,
            )
            return

        target = user or interaction.user
        # Adding or removing someone else requires elevation; self-service is open.
        if target.id != interaction.user.id and not await self._can_manage_others(interaction, row["host_id"]):
            await interaction.response.send_message(
                "Only the game's host, a moderator, or a Game-Host-role holder "
                "can add or remove other players.",
                ephemeral=True,
            )
            return

        game_type = row["game_type"]
        registry = self.bot.game_joiners if joining else self.bot.game_leavers
        handler = registry.get(game_type)
        if handler is None:
            if game_type in self.OPEN_SUBMISSION_GAMES:
                await interaction.response.send_message(
                    f"**{game_type}** is open to everyone — no need to {verb}; "
                    "just take part when a round opens.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"You can't {verb} **{game_type}** mid-game.", ephemeral=True,
                )
            return

        ok, message = await handler(interaction.channel, row["game_id"], target)
        # Announce successes in-channel so the room sees the roster change;
        # keep failures (already in/not in) private to the caller.
        await interaction.response.send_message(message, ephemeral=not ok)

    @app_commands.command(name="join", description="Join the game running in this channel.")
    @app_commands.describe(user="Add someone else (host/mod/game-host only). Omit to join yourself.")
    async def games_join(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await self._membership_command(interaction, user, joining=True)

    @app_commands.command(name="leave", description="Leave the game running in this channel.")
    @app_commands.describe(user="Remove someone else (host/mod/game-host only). Omit to leave yourself.")
    async def games_leave(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await self._membership_command(interaction, user, joining=False)

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
    bot.tree.remove_command("join")
    bot.tree.remove_command("leave")
    games.add_command(cog.config_group)
    games.add_command(cog.games_end)
    games.add_command(cog.games_join)
    games.add_command(cog.games_leave)
    bot.tree.add_command(games)
