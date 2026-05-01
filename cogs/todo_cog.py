from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.jail_commands import _is_mod
from services.todo_service import create_todo

if TYPE_CHECKING:
    from app_context import AppContext, Bot


_MAX_CONTENT_LEN = 1500
_NO_TEXT_MARKER = "[no text content]"
_NOTES_MAX_LEN = 1000
_TASK_MAX_LEN = 500


def _format_task_label(*, author_display: str, channel_name: str) -> str:
    """Build the headline shown in the todo list for a message-derived todo."""
    return f"Message from @{author_display} in #{channel_name}"


def _format_description(*, message_content: str, notes: str) -> str:
    """Build the description column: message content (truncated) then notes below.

    Either part may be empty. If the message has no text, '[no text content]' is
    used so the source link still has framing.
    """
    head = (message_content or "").strip()
    if len(head) > _MAX_CONTENT_LEN:
        head = head[:_MAX_CONTENT_LEN] + "…"
    if not head:
        head = _NO_TEXT_MARKER
    notes = notes.strip() if notes else ""
    if not notes:
        return head
    return f"{head}\n\n{notes}"


class _TodoFromMessageModal(discord.ui.Modal, title="Add to Todo"):
    notes: discord.ui.TextInput = discord.ui.TextInput(
        label="Notes",
        style=discord.TextStyle.paragraph,
        max_length=_NOTES_MAX_LEN,
        required=False,
        placeholder="Optional context for this todo",
    )

    def __init__(self, *, message: discord.Message, ctx: AppContext) -> None:
        super().__init__()
        self._message = message
        self._ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        author_display = getattr(self._message.author, "display_name", "unknown")
        channel_name = getattr(self._message.channel, "name", "unknown")
        task = _format_task_label(author_display=author_display, channel_name=channel_name)
        if len(task) > _TASK_MAX_LEN:
            task = task[: _TASK_MAX_LEN - 1] + "…"

        description = _format_description(
            message_content=self._message.content or "",
            notes=str(self.notes.value or ""),
        )

        with self._ctx.open_db() as conn:
            todo_id = create_todo(
                conn,
                interaction.guild.id,
                interaction.user.id,
                task,
                description=description,
                source_message_url=self._message.jump_url,
            )

        await interaction.response.send_message(
            f"Todo #{todo_id} added.", ephemeral=True
        )


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        ctx = self.ctx

        async def _add_to_todo_ctx(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            member = interaction.user
            if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
                await interaction.response.send_message("Mod only.", ephemeral=True)
                return
            await interaction.response.send_modal(
                _TodoFromMessageModal(message=message, ctx=ctx)
            )

        ctx_menu = app_commands.ContextMenu(
            name="Add to Todo", callback=_add_to_todo_ctx
        )
        ctx_menu.default_permissions = discord.Permissions(manage_messages=True)
        self.bot.tree.add_command(ctx_menu)
        self._add_to_todo_context_menu = ctx_menu

    async def cog_unload(self) -> None:
        if hasattr(self, "_add_to_todo_context_menu"):
            self.bot.tree.remove_command(
                "Add to Todo", type=discord.AppCommandType.message
            )

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
        if len(task) > _TASK_MAX_LEN:
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
