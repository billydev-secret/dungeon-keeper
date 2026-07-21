"""Watch list commands and message monitoring."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.watch_service import add_watched_user, remove_watched_user
from bot_modules.services.replies import NO_PERMISSION

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.watch")


class WatchCog(commands.Cog):
    watch = app_commands.Group(
        name="watch",
        description="Monitor a member — AI flags rule-concerning posts to your DMs.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @watch.command(
        name="add",
        description="Start watching a member. The AI screens their posts and DMs you when it flags a concern.",
    )
    @app_commands.describe(user="Member to watch.")
    async def watch_add(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                NO_PERMISSION, ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message(
                "❌ You cannot watch bots.", ephemeral=True
            )
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot watch yourself.", ephemeral=True
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ This command must be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        watcher_id = interaction.user.id
        watched_id = user.id

        def _do_add_watched():
            with ctx.open_db() as conn:
                add_watched_user(conn, guild_id, watched_id, watcher_id)

        await asyncio.to_thread(_do_add_watched)

        ctx.watched_users.setdefault(watched_id, set()).add(watcher_id)

        from bot_modules.services import ollama_client
        if ollama_client.is_available():
            note = "The AI will screen their posts and DM you only when it flags a rule concern."
        else:
            note = "AI filtering is not available — all their public posts will be DM'd to you."
        await interaction.response.send_message(
            f"Now watching {user.mention}. {note}",
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
                NO_PERMISSION, ephemeral=True
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ This command must be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        watcher_id = interaction.user.id
        watched_id = user.id

        def _do_remove_watched():
            with ctx.open_db() as conn:
                remove_watched_user(conn, guild_id, watched_id, watcher_id)

        await asyncio.to_thread(_do_remove_watched)

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
                NO_PERMISSION, ephemeral=True
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


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id not in self.ctx.watched_users:
            return
        await self._notify_watchers(message)

    async def _notify_watchers(self, message: discord.Message) -> None:
        watchers = list(self.ctx.watched_users.get(message.author.id, set()))
        if not watchers:
            return

        reason = ""
        from bot_modules.services import ollama_client
        if ollama_client.is_available():
            from bot_modules.services.ai_moderation_service import ai_check_watched_message
            try:
                is_violation, reason = await ai_check_watched_message(
                    message, db_path=self.ctx.db_path
                )
            except Exception as exc:
                log.warning(
                    "AI watch check failed for %s: %s — notifying anyway.",
                    message.author.display_name,
                    exc,
                )
                is_violation = True
            if not is_violation:
                return

        channel_name = getattr(message.channel, "name", str(message.channel.id))
        guild_name = message.guild.name if message.guild else "Unknown Server"

        body = message.content or "*[no text content]*"
        attachment_lines = "\n".join(a.url for a in message.attachments)
        rule_line = f"\n⚠️ **Rule concern:** {reason}" if reason else ""
        footer = (f"{attachment_lines}\n" if attachment_lines else "") + (
            f"— **{message.author.display_name}** (@{message.author.name}) "
            f"in **{guild_name}** / #{channel_name}\n"
            f"{message.jump_url}"
        )
        dm_text = f"{body}{rule_line}\n\n{footer}"

        for watcher_id in watchers:
            try:
                watcher = (
                    self.bot.get_user(watcher_id) or await self.bot.fetch_user(watcher_id)
                )
            except discord.HTTPException as exc:
                log.warning(
                    "Could not fetch watcher (id=%s) while relaying post from %s: %s",
                    watcher_id,
                    message.author.display_name,
                    exc,
                )
                continue
            try:
                await watcher.send(dm_text)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "Could not DM watcher %s for watched user %s: %s",
                    watcher.display_name,
                    message.author.display_name,
                    exc,
                )


async def setup(bot: Bot) -> None:
    await bot.add_cog(WatchCog(bot, bot.ctx))
