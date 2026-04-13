"""Invite link command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_invite_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="invite",
        description="Get a link to invite this bot to your server.",
    )
    async def invite(interaction: discord.Interaction):
        perms = discord.Permissions(
            manage_roles=True,
            manage_channels=True,
            manage_nicknames=True,
            kick_members=True,
            ban_members=True,
            moderate_members=True,
            manage_messages=True,
            read_messages=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            add_reactions=True,
            use_external_emojis=True,
        )
        assert bot.user is not None
        url = discord.utils.oauth_url(
            bot.user.id,
            permissions=perms,
            scopes=["bot", "applications.commands"],
        )
        await interaction.response.send_message(
            f"[Click here to invite me to your server]({url})",
            ephemeral=True,
        )
