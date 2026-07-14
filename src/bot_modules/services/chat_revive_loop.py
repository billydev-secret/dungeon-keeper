"""Chat Revive monitor loop — silence detection, posting, follow-up measuring.

Registered as a startup task factory (see ``__main__.py``); one ~2-minute tick
sweeps the follow-up measurements, then evaluates every enabled channel through
the same ``evaluate()`` brain the ``/revive check`` preview uses. Every SQLite
touch runs in ``asyncio.to_thread`` — nothing here blocks the event loop.

Zero-embarrassment posting: immediately before sending, the channel's newest
message is re-fetched — if it's the bot's own, or newer than the silence the
decision was based on, the revive is aborted. Combined with the in-flight
guard (shared with ``/revive fire``) this defeats ingest-lag and tick races.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot_modules.chat_revive.actions import channel_is_busy, send_revive
from bot_modules.chat_revive.logic import FLOURISHES, should_ping
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.question_source import channel_allows_nsfw
from bot_modules.services.chat_revive_service import (
    ChannelConfig,
    Evaluation,
    Question,
    evaluate,
    list_enabled_channels,
    measure_due_events,
    pick_question,
    record_event,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.chat_revive")

TICK_SECONDS = 120.0

# Channels with a revive send in progress (event-loop-confined, so a plain
# set is race-free). Shared by the loop and /revive fire.
_in_flight: set[int] = set()


class ReviveInFlight(Exception):
    """Another revive send is mid-flight for this channel."""


@contextlib.asynccontextmanager
async def send_guard(channel_id: int) -> AsyncIterator[None]:
    if channel_id in _in_flight:
        raise ReviveInFlight(str(channel_id))
    _in_flight.add(channel_id)
    try:
        yield
    finally:
        _in_flight.discard(channel_id)


# ── sync wrappers (run via asyncio.to_thread; also used by the cog) ──


def evaluate_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    *,
    now_ts: float,
    busy: bool,
    slowmode_delay: int,
) -> Evaluation:
    with open_db(db_path) as conn:
        return evaluate(
            conn,
            guild_id,
            channel_id,
            now_ts=now_ts,
            busy=busy,
            slowmode_delay=slowmode_delay,
        )


def pick_sync(
    db_path: Path,
    guild_id: int,
    *,
    categories: tuple[str, ...],
    allow_nsfw: bool,
    now_ts: float,
) -> Question | None:
    with open_db(db_path) as conn:
        return pick_question(
            conn, guild_id, categories=categories, allow_nsfw=allow_nsfw, now_ts=now_ts
        )


def record_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    *,
    question_id: int | None,
    message_id: int | None,
    trigger_kind: str,
    pinged: bool,
    now_ts: float,
    offset_hours: float,
) -> None:
    with open_db(db_path) as conn:
        record_event(
            conn,
            guild_id,
            channel_id,
            question_id=question_id,
            message_id=message_id,
            trigger_kind=trigger_kind,
            pinged=pinged,
            now_ts=now_ts,
            offset_hours=offset_hours,
        )


def _measure_sync(db_path: Path, now_ts: float) -> int:
    with open_db(db_path) as conn:
        return measure_due_events(conn, now_ts)


def _enabled_channels_sync(db_path: Path) -> list[ChannelConfig]:
    with open_db(db_path) as conn:
        return list_enabled_channels(conn)


# ── the monitor ───────────────────────────────────────────────────────


async def _lull_still_stands(
    bot: Bot, channel: discord.TextChannel, ev: Evaluation
) -> bool:
    """Re-check the channel's newest message right before sending.

    The decision was made from the ingest ledger, which can lag Discord by a
    beat. If the newest live message is the bot's own, or newer than the
    silence we judged, the lull is gone — stay silent.
    """
    try:
        newest = [m async for m in channel.history(limit=1)]
    except discord.HTTPException:
        log.exception("history re-check failed in #%s", channel.name)
        return False
    if not newest:
        return True
    msg = newest[0]
    bot_user = getattr(bot, "user", None)
    if bot_user is not None and msg.author.id == bot_user.id:
        return False  # never talk after ourselves
    last_seen = ev.inputs.last_human_ts or 0.0
    return msg.created_at.timestamp() <= last_seen + 1.0


async def consider_channel(
    bot: Bot, db_path: Path, cfg: ChannelConfig, now_ts: float
) -> bool:
    """Evaluate one enabled channel; post if a genuine lull passes every gate.

    Returns True only when a revive was actually sent and recorded.
    """
    guild = bot.get_guild(cfg.guild_id)
    channel = guild.get_channel(cfg.channel_id) if guild else None
    if not isinstance(channel, discord.TextChannel):
        return False
    busy = await channel_is_busy(bot, cfg.channel_id)
    ev = await asyncio.to_thread(
        evaluate_sync,
        db_path,
        cfg.guild_id,
        cfg.channel_id,
        now_ts=now_ts,
        busy=busy,
        slowmode_delay=channel.slowmode_delay or 0,
    )
    if not ev.verdict.fire:
        return False
    question = await asyncio.to_thread(
        pick_sync,
        db_path,
        cfg.guild_id,
        categories=cfg.categories,
        allow_nsfw=channel_allows_nsfw(channel),
        now_ts=now_ts,
    )
    if question is None:
        log.info(
            "revive lull in #%s but no eligible question (guild %s)",
            channel.name,
            cfg.guild_id,
        )
        return False
    role_id = cfg.role_id_override or ev.guild_cfg.role_id
    ping = bool(
        cfg.ping_enabled and role_id and should_ping(ev.freq.last_ping_ts, now_ts)
    )
    flourish = random.choice(FLOURISHES) if ev.guild_cfg.flourish_enabled else None
    try:
        async with send_guard(channel.id):
            if not await _lull_still_stands(bot, channel, ev):
                return False
            msg = await send_revive(
                channel,
                question_text=question.text,
                role_id=role_id if ping else None,
                flourish=flourish,
            )
    except ReviveInFlight:
        return False
    except discord.HTTPException:
        log.exception("revive send failed in #%s", channel.name)
        return False
    await asyncio.to_thread(
        record_sync,
        db_path,
        cfg.guild_id,
        cfg.channel_id,
        question_id=question.id,
        message_id=msg.id,
        trigger_kind="auto",
        pinged=ping,
        now_ts=now_ts,
        offset_hours=ev.offset_hours,
    )
    log.info(
        "revived #%s (guild %s): %r%s",
        channel.name,
        cfg.guild_id,
        question.text[:60],
        " +ping" if ping else "",
    )
    return True


async def run_tick(bot: Bot, db_path: Path, now_ts: float) -> None:
    """One tick: measure closed 30-minute windows, then sweep the channels."""
    try:
        await asyncio.to_thread(_measure_sync, db_path, now_ts)
    except Exception:
        log.exception("revive measurement sweep failed")
    try:
        channels = await asyncio.to_thread(_enabled_channels_sync, db_path)
    except Exception:
        log.exception("revive channel listing failed")
        return
    for cfg in channels:
        try:
            await consider_channel(bot, db_path, cfg, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "revive tick failed for channel %s (guild %s)",
                cfg.channel_id,
                cfg.guild_id,
            )


async def chat_revive_loop(bot: Bot, db_path: Path) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await run_tick(bot, db_path, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("chat revive tick crashed")
        await asyncio.sleep(TICK_SECONDS)
