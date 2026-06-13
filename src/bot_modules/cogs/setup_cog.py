"""One-shot /setup — walks through role/category config wizard."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.jail_commands import _setup_view

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


class SetupCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="setup",
        description="Configure roles and categories for the moderation system.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx

        if not ctx.is_admin(interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return

        embed, view = _setup_view(ctx, 1)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(SetupCog(bot, bot.ctx))
