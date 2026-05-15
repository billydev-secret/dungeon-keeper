import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, SUCCESS_COLOR, ERROR_COLOR

log = logging.getLogger(__name__)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


def is_mod_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        perms = interaction.user.guild_permissions
        return perms.administrator or perms.manage_guild or perms.manage_channels
    return app_commands.check(predicate)


class GamesConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    games_group = app_commands.Group(name="games", description="Bot configuration commands (admin only).")

    @games_group.command(name="allow-channel", description="Add the current channel to allowed game channels.")
    @is_admin()
    async def allow_channel(self, interaction: discord.Interaction):
        log.info("%s used /games allow-channel in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        try:
            await self.db.execute(
                "INSERT OR IGNORE INTO games_allowed_channels (channel_id, added_by) VALUES (?, ?)",
                (interaction.channel_id, interaction.user.id),
            )
            embed = discord.Embed(
                title="✅ Channel Allowed",
                description=f"{interaction.channel.mention} is now a game channel.",
                color=SUCCESS_COLOR,
            )
        except Exception as e:
            embed = discord.Embed(title="Error", description=str(e), color=ERROR_COLOR)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="disallow-channel", description="Remove the current channel from game channels.")
    @is_admin()
    async def disallow_channel(self, interaction: discord.Interaction):
        log.info("%s used /games disallow-channel in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        await self.db.execute(
            "DELETE FROM games_allowed_channels WHERE channel_id = ?",
            (interaction.channel_id,),
        )
        embed = discord.Embed(
            title="✅ Channel Removed",
            description=f"{interaction.channel.mention} is no longer a game channel.",
            color=SUCCESS_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="list-channels", description="List all allowed game channels.")
    @is_admin()
    async def list_channels(self, interaction: discord.Interaction):
        log.info("%s used /games list-channels in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        rows = await self.db.fetchall("SELECT channel_id FROM games_allowed_channels")
        if not rows:
            desc = "No game channels configured yet. Use `/games allow-channel`."
        else:
            mentions = []
            for row in rows:
                ch = interaction.guild.get_channel(row[0])
                mentions.append(ch.mention if ch else f"<#{row[0]}>")
            desc = "\n".join(mentions)
        embed = discord.Embed(
            title="Game Channels",
            description=desc,
            color=GOLDEN_MEADOW_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="game-status", description="Show the active game in this channel.")
    @is_mod_or_admin()
    async def game_status(self, interaction: discord.Interaction):
        log.info("%s used /games game-status in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        from bot_modules.games.utils.game_manager import get_active_game
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            embed = discord.Embed(
                title="No Active Game",
                description="There's no game running in this channel.",
                color=GOLDEN_MEADOW_COLOR,
            )
        else:
            embed = discord.Embed(
                title="Active Game",
                description=(
                    f"**Type:** {row['game_type']}\n"
                    f"**State:** {row['state']}\n"
                    f"**Host:** <@{row['host_id']}>\n"
                    f"**Game ID:** `{row['game_id']}`"
                ),
                color=GOLDEN_MEADOW_COLOR,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="game-end", description="Force-close the active game in this channel.")
    @is_mod_or_admin()
    async def game_end(self, interaction: discord.Interaction):
        log.info("%s used /games game-end in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
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
        embed = discord.Embed(
            title="🛑 Game Force-Closed",
            description=f"The **{row['game_type']}** game has been ended by an admin/mod.",
            color=ERROR_COLOR,
        )
        await interaction.followup.send(embed=embed)

    @games_group.command(name="audit-channel", description="Set or clear the audit log channel for anonymous submissions.")
    @is_admin()
    @app_commands.describe(channel="The channel to send audit logs to. Leave blank to clear.")
    async def audit_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        log.info("%s used /games audit-channel in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if channel:
            await self.db.execute(
                "INSERT INTO games_audit_channel (guild_id, channel_id, set_by) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = ?, set_by = ?",
                (interaction.guild_id, channel.id, interaction.user.id, channel.id, interaction.user.id),
            )
            embed = discord.Embed(
                title="✅ Audit Channel Set",
                description=f"Anonymous submissions will be logged to {channel.mention}.",
                color=SUCCESS_COLOR,
            )
        else:
            await self.db.execute(
                "DELETE FROM games_audit_channel WHERE guild_id = ?",
                (interaction.guild_id,),
            )
            embed = discord.Embed(
                title="✅ Audit Channel Cleared",
                description="Anonymous submission logging has been disabled.",
                color=SUCCESS_COLOR,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="portal-grant", description="Grant a user access to the web admin portal.")
    @is_admin()
    @app_commands.describe(user="The user to grant portal access to.")
    async def portal_grant(self, interaction: discord.Interaction, user: discord.User):
        log.info("%s used /games portal-grant for %s", interaction.user.display_name, user.display_name)
        await self.db.execute(
            "INSERT OR REPLACE INTO games_portal_access (user_id, granted_by) VALUES (?, ?)",
            (user.id, interaction.user.id),
        )
        embed = discord.Embed(
            title="✅ Portal Access Granted",
            description=f"{user.mention} can now sign in to the admin portal.",
            color=SUCCESS_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="portal-revoke", description="Revoke a user's web admin portal access.")
    @is_admin()
    @app_commands.describe(user="The user to revoke portal access from.")
    async def portal_revoke(self, interaction: discord.Interaction, user: discord.User):
        log.info("%s used /games portal-revoke for %s", interaction.user.display_name, user.display_name)
        await self.db.execute(
            "DELETE FROM games_portal_access WHERE user_id = ?",
            (user.id,),
        )
        embed = discord.Embed(
            title="✅ Portal Access Revoked",
            description=f"{user.mention} can no longer sign in to the admin portal.",
            color=SUCCESS_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @games_group.command(name="portal-list", description="List all users with web admin portal access.")
    @is_admin()
    async def portal_list(self, interaction: discord.Interaction):
        log.info("%s used /games portal-list", interaction.user.display_name)
        rows = await self.db.fetchall(
            "SELECT user_id, granted_by, granted_at FROM games_portal_access ORDER BY granted_at DESC"
        )
        if not rows:
            desc = "No users have been granted portal access. Use `/games portal-grant`."
        else:
            lines = []
            for row in rows:
                lines.append(f"<@{row[0]}> — granted by <@{row[1]}>")
            desc = "\n".join(lines)
        embed = discord.Embed(
            title="Portal Access List",
            description=desc,
            color=GOLDEN_MEADOW_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @allow_channel.error
    @disallow_channel.error
    @list_channels.error
    @audit_channel.error
    @portal_grant.error
    @portal_revoke.error
    @portal_list.error
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
    await bot.add_cog(GamesConfigCog(bot))
