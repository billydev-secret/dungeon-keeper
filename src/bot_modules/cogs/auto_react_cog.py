"""AutoReact cog — add configured emoji reactions to images posted in specific channels."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.services import nsfw_classifier_service
from bot_modules.services.auto_react_service import (
    get_auto_react_rule,
    parse_emojis,
    record_placement,
    should_place_tip_emoji,
)
from bot_modules.services.reaction_tip_service import apply_tip

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.auto_react")


def _image_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [
        att
        for att in message.attachments
        if att.content_type and att.content_type.startswith("image/")
    ]


def _has_image(message: discord.Message) -> bool:
    if _image_attachments(message):
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

        if int(row["tips_enabled"]):
            emojis = await self._tip_emojis_for(message, emojis)
            if not emojis:
                return

        results = await asyncio.gather(
            *(message.add_reaction(emoji) for emoji in emojis),
            return_exceptions=True,
        )
        for emoji, result in zip(emojis, results):
            if isinstance(result, Exception):
                log.warning("auto_react: failed to add %r in %d: %s", emoji, message.channel.id, result)

        if int(row["tips_enabled"]):
            placed = [e for e, r in zip(emojis, results) if not isinstance(r, Exception)]
            if placed:
                await self._record_placement(message, placed)

    async def _tip_emojis_for(
        self, message: discord.Message, emojis: list[str]
    ) -> list[str]:
        """Classify the post and decide whether tip emoji may be placed.

        Glue — the gate itself is auto_react_service.should_place_tip_emoji.
        Attachments only: an embed's image lives on an arbitrary external host
        and is never fetched, so a tipping channel places nothing on one.
        """
        age_gated = nsfw_classifier_service.is_age_gated_channel(message.channel)
        if not age_gated:
            # Checked before classifying, not after: it skips the download
            # entirely, and classifying here would record a row for a channel
            # that is out of recording scope.
            return []

        verdicts: list[bool | None] = []
        for attachment in _image_attachments(message):
            result = await nsfw_classifier_service.classify_for(
                self.ctx.db_path,
                attachment,
                guild_id=message.guild.id if message.guild else 0,
                channel_id=message.channel.id,
                message_id=message.id,
                channel_is_nsfw=age_gated,
            )
            verdicts.append(result.verdict)

        allowed = should_place_tip_emoji(
            channel_is_nsfw=age_gated, verdicts=verdicts
        )
        return emojis if allowed else []

    async def _record_placement(
        self, message: discord.Message, emojis: list[str]
    ) -> None:
        try:
            await asyncio.to_thread(
                record_placement,
                self.ctx.db_path,
                guild_id=message.guild.id if message.guild else 0,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                emojis=emojis,
            )
        except Exception:
            # Without the receipt these emoji simply aren't tippable — worth a
            # loud log, but never worth breaking the listener over.
            log.exception(
                "auto_react: failed to record placement for message %s", message.id
            )


    @commands.Cog.listener("on_raw_reaction_add")
    async def _on_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        """Charge a tip when someone taps an emoji the bot placed.

        Raw rather than ``on_reaction_add`` so reactions on messages missing
        from the cache still pay. Glue — every guard lives in
        reaction_tip_service.apply_tip so the decline reasons are testable.
        """
        if payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        member = payload.member
        try:
            outcome = await asyncio.to_thread(
                apply_tip,
                self.ctx.db_path,
                guild_id=payload.guild_id,
                message_id=payload.message_id,
                reactor_id=payload.user_id,
                emoji=str(payload.emoji),
                reactor_is_bot=bool(member and member.bot),
            )
        except Exception:
            # Money moves here, so a failure is worth an exception log — but
            # never worth taking down the reaction listener.
            log.exception(
                "tip: failed to apply tip for %s on message %s",
                payload.user_id,
                payload.message_id,
            )
            return

        if outcome.charged:
            log.info(
                "tip: %s paid %d (%d delivered, %d burned) on message %s",
                payload.user_id,
                outcome.paid,
                outcome.delivered,
                outcome.burned,
                payload.message_id,
            )


async def setup(bot: Bot) -> None:
    await bot.add_cog(AutoReactCog(bot, bot.ctx))
