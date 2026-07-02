"""Starboard cog — reposts starred messages to a dedicated channel."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from bot_modules.core.db_utils import get_config_id_set
from bot_modules.services.starboard_service import (
    add_reactor,
    delete_starboard_post,
    get_effective_star_count,
    get_starboard_config,
    get_starboard_post,
    insert_starboard_post,
    remove_reactor,
    update_starboard_post_count,
)
from bot_modules.starboard.embeds import (
    build_starboard_embed,
    updated_starboard_embed,
)
from bot_modules.starboard.filters import (
    nsfw_leak_blocked,
    should_process_reaction,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.starboard")

_EXCLUDED_BUCKET = "starboard_excluded_channels"


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class StarboardCog(commands.Cog):
    """Listener-only cog; starboard configuration lives in the web dashboard."""

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def _fetch_sb_message(self, channel_id: int, message_id: int) -> Optional[discord.Message]:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            log.warning("starboard: failed to fetch starboard message %s", message_id)
            return None

    # ------------------------------------------------------------------
    # Reaction events
    # ------------------------------------------------------------------

    # Per-guild: every listener resolves starboard config via
    # ``get_starboard_config(conn, payload.guild_id)`` and early-returns when a
    # guild has no enabled starboard, so the cog works across all guilds.

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return

        guild_id = payload.guild_id
        message_id = payload.message_id

        # Config read + reactor bookkeeping run off the event loop. Returns None
        # to signal "nothing to do" (no starboard, or this reaction is ignored).
        def _prep():
            with self.ctx.open_db() as conn:
                cfg = get_starboard_config(conn, guild_id)
                if cfg is None:
                    return None
                sb_channel_id = cfg["channel_id"]
                excluded = get_config_id_set(conn, _EXCLUDED_BUCKET, guild_id)
                if not should_process_reaction(
                    cfg_enabled=bool(cfg["enabled"]),
                    cfg_channel_id=sb_channel_id,
                    cfg_emoji=cfg["emoji"],
                    payload_emoji=str(payload.emoji),
                    payload_channel_id=payload.channel_id,
                    excluded_channel_ids=excluded,
                ):
                    return None
                add_reactor(conn, guild_id, message_id, payload.user_id)
                existing_post = get_starboard_post(conn, guild_id, message_id)
                return {
                    "emoji": cfg["emoji"],
                    "threshold": cfg["threshold"],
                    "sb_channel_id": sb_channel_id,
                    "existing_post": existing_post,
                }

        prep = await asyncio.to_thread(_prep)
        if prep is None:
            return
        emoji = prep["emoji"]
        threshold = prep["threshold"]
        sb_channel_id = prep["sb_channel_id"]
        existing_post = prep["existing_post"]

        # Existing post: just update the count, no need to fetch the original message.
        if existing_post is not None:
            author_id = int(existing_post["author_id"])
            sb_message_id = int(existing_post["starboard_message_id"])

            def _update_count():
                with self.ctx.open_db() as conn:
                    ec = get_effective_star_count(conn, guild_id, message_id, author_id)
                    update_starboard_post_count(conn, guild_id, message_id, ec)
                    return ec

            effective_count = await asyncio.to_thread(_update_count)
            sb_msg = await self._fetch_sb_message(sb_channel_id, sb_message_id)
            if sb_msg and sb_msg.embeds:
                await sb_msg.edit(
                    embed=updated_starboard_embed(sb_msg.embeds[0], effective_count, emoji),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            # Starboard message has been hand-deleted (or we lost access).
            # Drop the stale row so the rest of this handler can re-create
            # the post fresh below.
            def _drop_stale():
                with self.ctx.open_db() as conn:
                    delete_starboard_post(conn, guild_id, message_id)

            await asyncio.to_thread(_drop_stale)

        # No existing post — fetch original to get author and content.
        orig_channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(
            orig_channel,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread),
        ):
            return

        # NSFW leak guard: never repost an age-restricted source into a
        # non-age-restricted starboard channel. The starred message could
        # be visible to members who don't have access to the source.
        sb_channel = self.bot.get_channel(sb_channel_id)
        if not isinstance(sb_channel, discord.TextChannel):
            return
        if nsfw_leak_blocked(
            source_nsfw=bool(getattr(orig_channel, "nsfw", False)),
            starboard_nsfw=bool(getattr(sb_channel, "nsfw", False)),
        ):
            return

        try:
            message = await orig_channel.fetch_message(message_id)
        except (discord.NotFound, discord.HTTPException):
            return

        author_id = message.author.id

        def _check_threshold():
            with self.ctx.open_db() as conn:
                ec = get_effective_star_count(conn, guild_id, message_id, author_id)
                if ec < threshold:
                    return None
                # Guard against a concurrent reaction creating the post between our checks.
                if get_starboard_post(conn, guild_id, message_id) is not None:
                    return None
                return ec

        effective_count = await asyncio.to_thread(_check_threshold)
        if effective_count is None:
            return

        embed = build_starboard_embed(message, effective_count, emoji)
        try:
            sb_msg = await sb_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("starboard: failed to send starboard post for message %s", message_id)
            return

        def _insert():
            with self.ctx.open_db() as conn:
                insert_starboard_post(
                    conn, guild_id, message_id, sb_msg.id,
                    payload.channel_id, author_id, effective_count,
                )

        await asyncio.to_thread(_insert)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return

        guild_id = payload.guild_id
        message_id = payload.message_id

        def _prep_remove():
            with self.ctx.open_db() as conn:
                cfg = get_starboard_config(conn, guild_id)
                if cfg is None:
                    return None
                sb_channel_id = cfg["channel_id"]
                excluded = get_config_id_set(conn, _EXCLUDED_BUCKET, guild_id)
                if not should_process_reaction(
                    cfg_enabled=bool(cfg["enabled"]),
                    cfg_channel_id=sb_channel_id,
                    cfg_emoji=cfg["emoji"],
                    payload_emoji=str(payload.emoji),
                    payload_channel_id=payload.channel_id,
                    excluded_channel_ids=excluded,
                ):
                    return None
                remove_reactor(conn, guild_id, message_id, payload.user_id)
                existing_post = get_starboard_post(conn, guild_id, message_id)
                if existing_post is None:
                    return None
                author_id = int(existing_post["author_id"])
                sb_message_id = int(existing_post["starboard_message_id"])
                effective_count = get_effective_star_count(conn, guild_id, message_id, author_id)
                update_starboard_post_count(conn, guild_id, message_id, effective_count)
                return {
                    "emoji": cfg["emoji"],
                    "sb_channel_id": sb_channel_id,
                    "sb_message_id": sb_message_id,
                    "effective_count": effective_count,
                }

        prep = await asyncio.to_thread(_prep_remove)
        if prep is None:
            return
        emoji = prep["emoji"]
        sb_channel_id = prep["sb_channel_id"]
        sb_message_id = prep["sb_message_id"]
        effective_count = prep["effective_count"]

        sb_msg = await self._fetch_sb_message(sb_channel_id, sb_message_id)
        if sb_msg and sb_msg.embeds:
            await sb_msg.edit(
                embed=updated_starboard_embed(sb_msg.embeds[0], effective_count, emoji),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif sb_msg is None:
            # Starboard message gone — clean up the stale row so the next
            # reaction creates a fresh post instead of trying to edit None.
            def _drop_stale():
                with self.ctx.open_db() as conn:
                    delete_starboard_post(conn, guild_id, message_id)

            await asyncio.to_thread(_drop_stale)


async def setup(bot: Bot) -> None:
    await bot.add_cog(StarboardCog(bot, bot.ctx))
