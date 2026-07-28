"""Usage telemetry cog — records every slash command invocation.

Two hooks, because discord.py splits success from failure:

* ``on_app_command_completion`` fires once a command's callback returns cleanly
  → an ``ok=1`` row.
* ``tree.on_error`` is chained (not replaced) to catch invocations that raised
  → an ``ok=0`` row.

KNOWN GAP, deliberate: a cog that defines its own ``cog_app_command_error`` and
swallows the error — ``confessions_cog``, ``risky_roll_cog``, ``advisor_cog`` —
never reaches ``tree.on_error``, and its completion listener never fires either,
so a *failed* invocation of those commands records no row at all. It undercounts
errors rather than miscounting successes, which is the safe direction, and the
alternative was editing three unrelated cogs to re-raise. Revisit if those
commands' error rates become a question worth answering.

Panel views are recorded on the web side (``web_server/routes/telemetry.py``),
not here.

Writes are best-effort: telemetry must never be able to break the command it is
measuring, so every failure is logged and swallowed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.usage_telemetry_service import KIND_COMMAND, record_event

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.usage_telemetry")


def command_name(command: app_commands.Command | app_commands.ContextMenu | None) -> str:
    """Full command name, space-joined through any parent groups.

    ``/quest board`` records as ``"quest board"`` so the parent groups in the
    report instead of every subcommand looking like an unrelated command.
    """
    if command is None:
        return ""
    qualified = getattr(command, "qualified_name", None)
    return str(qualified or getattr(command, "name", "") or "")


class UsageTelemetryCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # Chain rather than replace: if something else already owns tree.on_error
        # we must not silently disable it.
        self._prev_tree_error = bot.tree.on_error
        bot.tree.on_error = self._on_tree_error  # type: ignore[method-assign]

    async def cog_unload(self) -> None:
        self.bot.tree.on_error = self._prev_tree_error  # type: ignore[method-assign]

    def _record(self, interaction: discord.Interaction, *, ok: bool) -> None:
        # DM invocations have no guild to attribute the row to. Dropping them
        # keeps every query guild-scoped like the rest of the dashboard.
        if interaction.guild_id is None:
            return
        name = command_name(interaction.command)
        if not name:
            return
        user = interaction.user
        if user is None or user.bot:
            return
        try:
            with self.ctx.open_db() as conn:
                record_event(
                    conn,
                    interaction.guild_id,
                    KIND_COMMAND,
                    name,
                    user.id,
                    channel_id=interaction.channel_id,
                    ok=ok,
                )
        except Exception:
            log.exception("usage telemetry: failed to record %r", name)

    @commands.Cog.listener("on_app_command_completion")
    async def _on_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        self._record(interaction, ok=True)

    async def _on_tree_error(
        self,
        # The tree's own handler is typed Interaction[Bot]; matching it keeps
        # the chained call below assignable.
        interaction: discord.Interaction[Bot],
        error: app_commands.AppCommandError,
    ) -> None:
        self._record(interaction, ok=False)
        await self._prev_tree_error(interaction, error)


async def setup(bot: Bot) -> None:
    await bot.add_cog(UsageTelemetryCog(bot, bot.ctx))
