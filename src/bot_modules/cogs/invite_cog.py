"""Invite link command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


class InviteCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="invite",
        description="Get a link to invite this bot to your server.",
    )
    async def invite(self, interaction: discord.Interaction) -> None:
        # Least-privilege set: every permission here maps to an API call the bot
        # actually makes. The bot never bans/kicks/timeouts (jail is role- and
        # channel-overwrite based), so those mod perms are deliberately absent.
        perms = discord.Permissions(
            manage_roles=True,  # jail, xp, whisper, guess, wellness, dm_perms, booster
            manage_channels=True,  # jail/setup channels, Voice Master temp channels
            manage_nicknames=True,  # duels, pressure_cooker, quickdraw
            manage_messages=True,  # purge, auto-delete, post monitoring, pins
            move_members=True,  # Voice Master move/disconnect
            connect=True,  # music cog joins voice
            speak=True,  # music cog streams audio
            create_public_threads=True,  # confessions, needle, risky_roll
            send_messages_in_threads=True,
            manage_threads=True,  # needle deletes resolved threads
            read_messages=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            add_reactions=True,
            use_external_emojis=True,
        )
        assert self.bot.user is not None
        url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=perms,
            scopes=["bot", "applications.commands"],
        )
        await interaction.response.send_message(
            f"[Click here to invite me to your server]({url})",
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(InviteCog(bot, bot.ctx))
