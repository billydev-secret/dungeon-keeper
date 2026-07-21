"""`/ask` — member self-service help, answered by Billy-bot.

Thin glue over ``bot_modules.services.advisor_service``; the same brain powers
the dashboard Help panel's "Ask Billy-bot" box. Answers are grounded in the user
manual, so Billy-bot can't invent commands. Ephemeral + per-user cooldown so one
member can't spend the shared Anthropic budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import open_db
from bot_modules.services.advisor_context import build_asker_context
from bot_modules.services.advisor_service import (
    MODEL,
    answer_advisor,
    get_advisor_context_enabled,
    get_advisor_model,
)

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
        description="Ask Billy-bot how to use the server — games, commands, settings.",
    )
    @app_commands.describe(question="What do you want to know how to do?")
    @app_commands.checks.cooldown(1, 12.0, key=lambda i: i.user.id)
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        log.info("%s used /ask: %.80s", interaction.user.display_name, question)
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        model = MODEL
        guild_context: str | None = None
        if guild is not None:
            db_path = self.ctx.db_path
            with open_db(db_path) as conn:
                model = get_advisor_model(conn, guild.id)
                context_on = get_advisor_context_enabled(conn, guild.id)
            if context_on:
                member = (
                    interaction.user
                    if isinstance(interaction.user, discord.Member)
                    else None
                )
                guild_context = build_asker_context(guild, member, db_path)

        result = await answer_advisor(question, model=model, guild_context=guild_context)
        answer = result.answer
        if len(answer) > _MAX_DESC:
            answer = answer[:_MAX_DESC].rstrip() + "…"

        color = (
            await resolve_accent_color(self.ctx.db_path, interaction.guild)
            if interaction.guild
            else None
        )
        embed = discord.Embed(
            title="🤖 Billy-bot",
            description=answer,
            color=color,
        )
        embed.set_footer(text="Billy-bot • grounded in the server guide, not always perfect")
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
