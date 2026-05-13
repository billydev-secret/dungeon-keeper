"""Support server link command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from core.app_context import AppContext, Bot


SUPPORT_INVITE_URL = "https://discord.gg/7gfbYYkH"


class SupportCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="support",
        description="Get a link to the support Discord server.",
    )
    async def support(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"[Click here to join the support server]({SUPPORT_INVITE_URL})",
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(SupportCog(bot, bot.ctx))
