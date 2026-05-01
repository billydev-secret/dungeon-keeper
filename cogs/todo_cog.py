from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.todo_service import create_todo

if TYPE_CHECKING:
    from app_context import AppContext, Bot


_MAX_CONTENT_LEN = 1500
_NO_TEXT_MARKER = "[no text content]"


def _format_task_label(*, author_display: str, channel_name: str) -> str:
    """Build the headline shown in the todo list for a message-derived todo."""
    return f"Message from @{author_display} in #{channel_name}"


def _format_description(*, message_content: str, notes: str) -> str:
    """Build the description column: message content (truncated) then notes below.

    Either part may be empty. If the message has no text, '[no text content]' is
    used so the source link still has framing.
    """
    head = message_content
    if len(head) > _MAX_CONTENT_LEN:
        head = head[:_MAX_CONTENT_LEN] + "…"
    if not head:
        head = _NO_TEXT_MARKER
    notes = notes.strip() if notes else ""
    if not notes:
        return head
    return f"{head}\n\n{notes}"


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
            todo_id = create_todo(conn, interaction.guild.id, interaction.user.id, task)
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot, bot.ctx))
