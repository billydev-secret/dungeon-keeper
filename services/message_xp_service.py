"""Message XP award service - handles XP from text messages and reactions."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord

from db_utils import open_db
from post_monitoring import message_has_qualifying_image
from utils import format_user_for_log, get_guild_channel_or_thread, resolve_reply_target
from xp_system import (
    DEFAULT_XP_SETTINGS,
    XP_SOURCE_IMAGE_REACT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    AwardResult,
    MessageXpContext,
    XpSettings,
    apply_xp_award,
    calculate_message_xp,
    get_member_xp_state,
    is_channel_xp_eligible,
    mark_message_processed,
    normalize_message_content,
    record_xp_event,
    update_pair_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from xp_system import PairState

    GuildTextLike = discord.TextChannel | discord.Thread

log = logging.getLogger("dungeonkeeper.message_xp")


async def award_message_xp(
    message: discord.Message,
    bot: discord.Client,
    db_path: Path,
    xp_pair_states: dict[int, PairState],
    excluded_channel_ids: set[int],
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> AwardResult | None:
    """Award XP for a text message."""
    if not message.guild or not isinstance(message.author, discord.Member):
        return None

    channel = message.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return None

    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return None
    parent_id = getattr(channel, "parent_id", None)
    if not is_channel_xp_eligible(channel_id, parent_id, excluded_channel_ids):
        log.debug(
            "XP skipped for %s in #%s: channel excluded.",
            format_user_for_log(message.author),
            channel_id,
        )
        return None

    reply_target = await resolve_reply_target(message)
    is_reply_to_human = bool(
        reply_target
        and not reply_target.author.bot
        and reply_target.author.id != message.author.id
    )

    now_ts = message.created_at.timestamp() if message.created_at else time.time()
    normalized_content = normalize_message_content(message.content)
    pair_state = xp_pair_states.get(channel.id)
    next_pair_state, pair_streak = update_pair_state(pair_state, message.author.id)
    xp_pair_states[channel.id] = next_pair_state

    with open_db(db_path) as conn:
        state = get_member_xp_state(conn, message.guild.id, message.author.id, settings)
        is_duplicate = bool(normalized_content) and normalized_content == state.last_message_norm
        breakdown = calculate_message_xp(
            MessageXpContext(
                content=message.content,
                seconds_since_last_message=(
                    None if state.last_message_at is None else now_ts - state.last_message_at
                ),
                is_duplicate=is_duplicate,
                is_reply_to_human=is_reply_to_human,
                pair_streak=pair_streak,
            ),
            settings,
        )
        award = apply_xp_award(
            conn,
            message.guild.id,
            message.author.id,
            breakdown.awarded_xp,
            message_timestamp=now_ts,
            message_norm=breakdown.normalized_content,
            settings=settings,
        )
        reply_award = 0.0
        if breakdown.reply_bonus_xp > 0:
            reply_award = round(
                breakdown.reply_bonus_xp
                * breakdown.cooldown_multiplier
                * breakdown.duplicate_multiplier
                * breakdown.pair_multiplier,
                2,
            )
        text_award = round(max(0.0, award.awarded_xp - reply_award), 2)
        record_xp_event(
            conn,
            message.guild.id,
            message.author.id,
            XP_SOURCE_TEXT,
            text_award,
            now_ts,
        )
        record_xp_event(
            conn,
            message.guild.id,
            message.author.id,
            XP_SOURCE_REPLY,
            reply_award,
            now_ts,
        )
        mark_message_processed(
            conn,
            message.guild.id,
            message.id,
            message.channel.id,
            message.author.id,
            now_ts,
        )

    if award.awarded_xp <= 0:
        log.debug(
            "XP skipped for %s in #%s: zero award (words=%s duplicate=%s cooldown=%.2f pair=%.2f reply_bonus=%.2f).",
            format_user_for_log(message.author),
            channel_id,
            breakdown.qualified_words,
            is_duplicate,
            breakdown.cooldown_multiplier,
            breakdown.pair_multiplier,
            breakdown.reply_bonus_xp,
        )
        return None

    log.debug(
        "Awarded %.2f text XP to %s in #%s (words=%s total=%.2f level=%s).",
        award.awarded_xp,
        format_user_for_log(message.author),
        channel_id,
        breakdown.qualified_words,
        award.total_xp,
        award.new_level,
    )

    return award


async def award_image_reaction_xp(
    payload: discord.RawReactionActionEvent,
    bot: discord.Client,
    db_path: Path,
    excluded_channel_ids: set[int],
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> tuple[discord.Member, AwardResult] | None:
    """Award XP to image poster when their image receives a reaction."""
    bot_user = bot.user
    if payload.guild_id is None or bot_user is None or payload.user_id == bot_user.id:
        return None

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return None

    channel = get_guild_channel_or_thread(guild, payload.channel_id)
    if channel is None:
        return None

    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return None
    parent_id = getattr(channel, "parent_id", None)
    if not is_channel_xp_eligible(channel_id, parent_id, excluded_channel_ids):
        return None

    member = guild.get_member(payload.user_id)
    if member is not None and member.bot:
        return None

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.Forbidden, discord.NotFound):
        return None

    if not isinstance(message.author, discord.Member):
        author = guild.get_member(message.author.id)
        if author is None:
            return None
    else:
        author = message.author

    if author.bot or author.id == payload.user_id:
        return None

    if not message_has_qualifying_image(message):
        return None

    with open_db(db_path) as conn:
        award = apply_xp_award(
            conn,
            guild.id,
            author.id,
            settings.image_reaction_received_xp,
            event_source=XP_SOURCE_IMAGE_REACT,
            settings=settings,
        )

    log.debug(
        "Awarded %.2f image reaction XP to %s for message %s from reaction by %s.",
        award.awarded_xp,
        format_user_for_log(author),
        message.id,
        payload.user_id,
    )

    return author, award
