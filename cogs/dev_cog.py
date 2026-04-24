"""Developer tools — hot-reload cog extensions."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from app_context import Bot


class DevCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _ext_autocomplete(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=name, value=name)
            for name in self.bot.extensions
            if current.lower() in name.lower()
        ][:25]

    @app_commands.command(name="reload_cog", description="Reload a cog extension.")
    @app_commands.describe(extension="Extension to reload, e.g. cogs.mod_cog")
    @app_commands.autocomplete(extension=_ext_autocomplete)
    async def reload_cog(self, interaction: discord.Interaction, extension: str) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Bot owner only.", ephemeral=True)
            return
        if extension not in self.bot.extensions:
            await interaction.response.send_message(
                f"Unknown extension `{extension}`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.reload_extension(extension)
        if self.bot.debug:
            guild = discord.Object(id=self.bot.guild_id)
            self.bot.tree.copy_global_to(guild=guild)
            await self.bot.tree.sync(guild=guild)
        else:
            await self.bot.tree.sync()
        await interaction.followup.send(f"Reloaded `{extension}`.", ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(DevCog(bot))
