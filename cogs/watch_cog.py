"""Watch list commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.watch_service import add_watched_user, remove_watched_user

if TYPE_CHECKING:
    from app_context import AppContext, Bot


class WatchCog(commands.Cog):
    watch = app_commands.Group(
        name="watch",
        description="Silently monitor a member's public messages via DM.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @watch.command(
        name="add",
        description="Start watching a member. Their messages are forwarded to your DMs.",
    )
    @app_commands.describe(user="Member to watch.")
    async def watch_add(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message(
                "You cannot watch bots.", ephemeral=True
            )
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot watch yourself.", ephemeral=True
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        watcher_id = interaction.user.id
        watched_id = user.id

        with ctx.open_db() as conn:
            add_watched_user(conn, guild_id, watched_id, watcher_id)

        ctx.watched_users.setdefault(watched_id, set()).add(watcher_id)

        await interaction.response.send_message(
            f"Now watching {user.mention}. Their public posts will be DM'd to you.",
            ephemeral=True,
        )

    @watch.command(name="remove", description="Stop watching a member.")
    @app_commands.describe(user="Member to stop watching.")
    async def watch_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        watcher_id = interaction.user.id
        watched_id = user.id

        with ctx.open_db() as conn:
            remove_watched_user(conn, guild_id, watched_id, watcher_id)

        if watched_id in ctx.watched_users:
            ctx.watched_users[watched_id].discard(watcher_id)
            if not ctx.watched_users[watched_id]:
                del ctx.watched_users[watched_id]

        await interaction.response.send_message(
            f"Stopped watching {user.mention}.", ephemeral=True
        )

    @watch.command(
        name="list", description="List everyone you are currently watching."
    )
    async def watch_list(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        watcher_id = interaction.user.id
        guild = interaction.guild

        watched_ids = [
            uid for uid, watchers in ctx.watched_users.items() if watcher_id in watchers
        ]

        if not watched_ids:
            await interaction.response.send_message(
                "You are not watching any users.", ephemeral=True
            )
            return

        labels = []
        for uid in sorted(watched_ids):
            member = guild.get_member(uid) if guild else None
            labels.append(member.mention if member else f"`{uid}`")

        await interaction.response.send_message(
            "You are currently watching: " + ", ".join(labels), ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(WatchCog(bot, bot.ctx))
