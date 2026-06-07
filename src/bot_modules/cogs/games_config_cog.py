import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games_config.embeds import (
    build_audit_channel_embed,
    build_channel_allowed_embed,
    build_channel_disallowed_embed,
    build_channel_list_embed,
    build_force_end_embed,
    build_game_status_embed,
)
from bot_modules.games_config.logic import (
    has_admin_permissions,
    has_mod_or_admin_permissions,
)
from bot_modules.games.command_groups import games
from bot_modules.games.constants import ERROR_COLOR

log = logging.getLogger(__name__)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        return has_admin_permissions(interaction.user.guild_permissions)
    return app_commands.check(predicate)


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
        description="Bot configuration commands (admin only).",
    )

    @config_group.command(name="allow-channel", description="Add the current channel to allowed game channels.")
    @is_admin()
    async def allow_channel(self, interaction: discord.Interaction):
        log.info("%s used /games config allow-channel in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        try:
            await self.db.execute(
                "INSERT OR IGNORE INTO games_allowed_channels (channel_id, added_by) VALUES (?, ?)",
                (interaction.channel_id, interaction.user.id),
            )
            embed = build_channel_allowed_embed(interaction.channel.mention)
        except Exception as e:
            embed = discord.Embed(title="Error", description=str(e), color=ERROR_COLOR)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="disallow-channel", description="Remove the current channel from game channels.")
    @is_admin()
    async def disallow_channel(self, interaction: discord.Interaction):
        log.info("%s used /games config disallow-channel in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        await self.db.execute(
            "DELETE FROM games_allowed_channels WHERE channel_id = ?",
            (interaction.channel_id,),
        )
        embed = build_channel_disallowed_embed(interaction.channel.mention)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="list-channels", description="List all allowed game channels.")
    @is_admin()
    async def list_channels(self, interaction: discord.Interaction):
        log.info("%s used /games config list-channels in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        rows = await self.db.fetchall("SELECT channel_id FROM games_allowed_channels")
        embed = build_channel_list_embed(rows, interaction.guild.get_channel)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    @config_group.command(name="audit-channel", description="Set or clear the audit log channel for anonymous submissions.")
    @is_admin()
    @app_commands.describe(channel="The channel to send audit logs to. Leave blank to clear.")
    async def audit_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        log.info("%s used /games config audit-channel in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if channel:
            await self.db.execute(
                "INSERT INTO games_audit_channel (guild_id, channel_id, set_by) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = ?, set_by = ?",
                (interaction.guild_id, channel.id, interaction.user.id, channel.id, interaction.user.id),
            )
            embed = build_audit_channel_embed(channel.id)
        else:
            await self.db.execute(
                "DELETE FROM games_audit_channel WHERE guild_id = ?",
                (interaction.guild_id,),
            )
            embed = build_audit_channel_embed(None)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @allow_channel.error
    @disallow_channel.error
    @list_channels.error
    @audit_channel.error
    async def admin_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            log.error("Permission denied for %s on admin command in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.", ephemeral=True
            )
        else:
            log.error("Error in config command: %s", error, exc_info=True)
            raise error

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
