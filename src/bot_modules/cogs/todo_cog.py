from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.services.todo_service import TASK_MAX_LEN, create_todo

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(name="todo", description="Add a task to the server todo list.")
    @app_commands.describe(task="The task to add.")
    async def todo(self, interaction: discord.Interaction, task: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        # The todo list is a mod worklist, curated from the dashboard — only
        # moderators may add to it (the web endpoints are mod-gated too).
        if not isinstance(
            interaction.user, discord.Member
        ) or not has_mod_or_admin_permissions(interaction.user.guild_permissions):
            await interaction.response.send_message(
                "❌ Only moderators can add to the todo list.", ephemeral=True
            )
            return
        task = task.strip()
        if not task:
            await interaction.response.send_message("❌ Task cannot be empty.", ephemeral=True)
            return
        if len(task) > TASK_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Task must be {TASK_MAX_LEN} characters or fewer.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        def _do_create_todo():
            with self.ctx.open_db() as conn:
                return create_todo(conn, guild_id, user_id, task)

        todo_id = await asyncio.to_thread(_do_create_todo)
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot, bot.ctx))
