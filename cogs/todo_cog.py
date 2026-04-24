from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(name="todo", description="Add a task to the server todo list.")
    @app_commands.describe(task="The task to add.")
    async def todo(self, interaction: discord.Interaction, task: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        task = task.strip()
        if not task:
            await interaction.response.send_message("Task cannot be empty.", ephemeral=True)
            return
        if len(task) > 500:
            await interaction.response.send_message(
                "Task must be 500 characters or fewer.", ephemeral=True
            )
            return
        with self.ctx.open_db() as conn:
            cur = conn.execute(
                "INSERT INTO todos (guild_id, added_by, task, created_at) VALUES (?, ?, ?, ?)",
                (interaction.guild.id, interaction.user.id, task, time.time()),
            )
            todo_id = cur.lastrowid
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot, bot.ctx))
