"""AutoReact cog — add configured emoji reactions to images posted in specific channels."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.services.auto_react_service import get_auto_react_rule, parse_emojis

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.auto_react")


def _has_image(message: discord.Message) -> bool:
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            return True
    for embed in message.embeds:
        if embed.type in ("image", "gifv", "rich") and (embed.image or embed.thumbnail):
            return True
    return False


class AutoReactCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return
        if not _has_image(message):
            return

        row = get_auto_react_rule(self.ctx.db_path, message.guild.id, message.channel.id)
        if not row or not int(row["enabled"]):
            return

        emojis = parse_emojis(row["emojis"])
        results = await asyncio.gather(
            *(message.add_reaction(emoji) for emoji in emojis),
            return_exceptions=True,
        )
        for emoji, result in zip(emojis, results):
            if isinstance(result, Exception):
                log.warning("auto_react: failed to add %r in %d: %s", emoji, message.channel.id, result)


async def setup(bot: Bot) -> None:
    await bot.add_cog(AutoReactCog(bot, bot.ctx))
