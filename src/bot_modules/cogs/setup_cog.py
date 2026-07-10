"""One-shot /setup — walks through role/category config wizard."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.jail_commands import _setup_dm_view, _setup_view
from bot_modules.core.branding import resolve_accent_color

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

        guild = interaction.guild
        if guild is None:  # guild_only() guarantees this, but keep the type-checker happy
            await interaction.response.send_message(
                "Run this in a server.", ephemeral=True
            )
            return

        # ACK immediately: opening a DM in a fresh server is two round-trips
        # (create channel + send) on top of the accent-colour DB read, which
        # can blow the 3s interaction deadline on the constrained prod box. If
        # the token expired *and* DMs were closed, both the DM and the fallback
        # below would fail on a dead token and setup would silently do nothing.
        await interaction.response.defer(ephemeral=True)

        accent = await resolve_accent_color(ctx.db_path, guild)

        # Preferred path: DM the admin who ran /setup and walk them through the
        # questions there. Falls back to the in-channel wizard if their DMs are
        # closed (a common setting), so setup is never a dead end.
        # discord.Forbidden is an HTTPException subclass, so this catches both.
        dm_embed, dm_view = _setup_dm_view(ctx, guild, colour=accent)
        try:
            await interaction.user.send(embed=dm_embed, view=dm_view)
        except discord.HTTPException:
            dm_view.stop()
            embed, view = _setup_view(ctx, 1, colour=accent)
            await interaction.followup.send(
                "⚠️ I couldn't DM you — your DMs may be closed. "
                "Let's do it here instead.",
                embed=embed, view=view, ephemeral=True,
            )
            return

        await interaction.followup.send(
            "📬 Check your DMs — I've sent the setup questions there.",
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(SetupCog(bot, bot.ctx))
