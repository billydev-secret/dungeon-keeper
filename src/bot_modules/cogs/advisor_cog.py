"""`/ask` — member self-service help, answered by the grounded AI advisor.

Thin glue over ``bot_modules.services.advisor_service``; the same brain powers
the dashboard Help panel's "Ask the Guide" box. Answers are grounded in the user
manual, so the advisor can't invent commands. Ephemeral + per-user cooldown so
one member can't spend the shared Anthropic budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.advisor_service import answer_advisor

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

# Discord embed descriptions cap at 4096 chars; leave room for the trailer.
_MAX_DESC = 3900


class AdvisorCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="ask",
        description="Ask how to use Dungeon Keeper — games, commands, settings.",
    )
    @app_commands.describe(question="What do you want to know how to do?")
    @app_commands.checks.cooldown(1, 12.0, key=lambda i: i.user.id)
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        log.info("%s used /ask: %.80s", interaction.user.display_name, question)
        await interaction.response.defer(ephemeral=True, thinking=True)

        result = await answer_advisor(question)
        answer = result.answer
        if len(answer) > _MAX_DESC:
            answer = answer[:_MAX_DESC].rstrip() + "…"

        color = (
            await resolve_accent_color(self.ctx.db_path, interaction.guild)
            if interaction.guild
            else None
        )
        embed = discord.Embed(
            title="📖 Dungeon Keeper Guide",
            description=answer,
            color=color,
        )
        embed.set_footer(text="Grounded in the server guide • not always perfect")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Give me a sec — try again in {error.retry_after:.0f}s."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        log.exception("Unexpected /ask error", exc_info=error)


async def setup(bot: Bot) -> None:
    await bot.add_cog(AdvisorCog(bot, bot.ctx))
