"""Greeting Watch monitor loop.

Registered as a startup task factory (see ``__main__.py``); one ~1-minute tick
finds greetings whose unanswered-window has closed and, for any where the
greeter was never acknowledged, DMs the configured notify user. Every SQLite
touch runs in ``asyncio.to_thread`` so nothing here blocks the event loop.

Config (``greeting_watch_*`` keys) is read straight from the DB each tick — like
Rules Watch — so an admin toggling it on the dashboard takes effect on the next
sweep without a restart or cache invalidation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_config_value, open_db, parse_bool
from bot_modules.services.greeting_watch_service import (
    PendingGreeting,
    guilds_with_pending,
    list_due_greetings,
    mark_resolved,
    was_acknowledged,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.greeting_watch")

TICK_SECONDS = 60.0
DEFAULT_WINDOW_MINUTES = 10


# ── sync wrappers (run via asyncio.to_thread) ──────────────────────────


def _load_settings(db_path: Path, guild_id: int) -> tuple[bool, int, int]:
    """Return (enabled, window_minutes, notify_user_id) for a guild."""
    with open_db(db_path) as conn:
        enabled = parse_bool(
            get_config_value(conn, "greeting_watch_enabled", "false", guild_id)
        )
        try:
            window = max(
                1,
                int(
                    get_config_value(
                        conn,
                        "greeting_watch_window_minutes",
                        str(DEFAULT_WINDOW_MINUTES),
                        guild_id,
                    )
                ),
            )
        except (TypeError, ValueError):
            window = DEFAULT_WINDOW_MINUTES
        try:
            notify = int(
                get_config_value(
                    conn, "greeting_watch_notify_user_id", "0", guild_id
                )
            )
        except (TypeError, ValueError):
            notify = 0
    return enabled, window, notify


def _guilds_with_pending_sync(db_path: Path) -> list[int]:
    with open_db(db_path) as conn:
        return guilds_with_pending(conn)


def _due_sync(db_path: Path, guild_id: int, cutoff_ts: int) -> list[PendingGreeting]:
    with open_db(db_path) as conn:
        return list_due_greetings(conn, guild_id, cutoff_ts)


def _acked_sync(
    db_path: Path, guild_id: int, author_id: int, since_ts: int, until_ts: int
) -> bool:
    with open_db(db_path) as conn:
        return was_acknowledged(conn, guild_id, author_id, since_ts, until_ts)


def _resolve_sync(
    db_path: Path, guild_id: int, message_ids: list[int], outcome: str, now_ts: int
) -> None:
    with open_db(db_path) as conn:
        for mid in message_ids:
            mark_resolved(conn, guild_id, mid, outcome, now_ts)


# ── the monitor ────────────────────────────────────────────────────────


async def _notify(
    bot: Bot,
    guild_id: int,
    notify_user_id: int,
    g: PendingGreeting,
    window_minutes: int,
) -> bool:
    """DM the notify user about one unanswered greeting. Returns send success."""
    user = bot.get_user(notify_user_id)
    if user is None:
        try:
            user = await bot.fetch_user(notify_user_id)
        except discord.HTTPException:
            log.warning(
                "greeting watch: cannot resolve notify user %s", notify_user_id
            )
            return False

    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(g.channel_id) if guild else None
    channel_name = getattr(channel, "name", None)
    channel_label = f"#{channel_name}" if channel_name else "the channel"
    greeter = guild.get_member(g.author_id) if guild else None
    greeter_label = greeter.display_name if greeter else f"<@{g.author_id}>"
    jump = f"https://discord.com/channels/{guild_id}/{g.channel_id}/{g.message_id}"

    text = (
        f"👋 **Someone may have been left hanging in {channel_label}**\n"
        f"{greeter_label} said hello about {window_minutes} min ago and nobody "
        f"has replied to or mentioned them since.\n{jump}"
    )
    try:
        await user.send(text)
        return True
    except discord.Forbidden:
        log.warning(
            "greeting watch: notify user %s has DMs closed", notify_user_id
        )
        return False
    except discord.HTTPException:
        log.exception("greeting watch: DM to %s failed", notify_user_id)
        return False


async def _process_guild(
    bot: Bot, db_path: Path, guild_id: int, now_ts: float
) -> None:
    enabled, window_minutes, notify_user_id = await asyncio.to_thread(
        _load_settings, db_path, guild_id
    )
    window_seconds = window_minutes * 60
    cutoff = int(now_ts - window_seconds)
    due = await asyncio.to_thread(_due_sync, db_path, guild_id, cutoff)
    if not due:
        return

    # Turned off (or notify user cleared) mid-window: retire the stragglers
    # quietly so they don't linger unresolved forever.
    if not enabled or not notify_user_id:
        await asyncio.to_thread(
            _resolve_sync,
            db_path,
            guild_id,
            [g.message_id for g in due],
            "skipped",
            int(now_ts),
        )
        return

    for g in due:
        acked = await asyncio.to_thread(
            _acked_sync,
            db_path,
            guild_id,
            g.author_id,
            g.created_ts,
            g.created_ts + window_seconds,
        )
        if acked:
            await asyncio.to_thread(
                _resolve_sync,
                db_path,
                guild_id,
                [g.message_id],
                "acknowledged",
                int(now_ts),
            )
            continue
        await _notify(bot, guild_id, notify_user_id, g, window_minutes)
        # Resolve whether or not the DM landed — a closed-DM failure would
        # otherwise wedge the row into re-alerting every tick. The failure is
        # already logged in _notify.
        await asyncio.to_thread(
            _resolve_sync,
            db_path,
            guild_id,
            [g.message_id],
            "unanswered",
            int(now_ts),
        )


async def run_tick(bot: Bot, db_path: Path, now_ts: float) -> None:
    try:
        guild_ids = await asyncio.to_thread(_guilds_with_pending_sync, db_path)
    except Exception:
        log.exception("greeting watch: pending-guild scan failed")
        return
    for guild_id in guild_ids:
        try:
            await _process_guild(bot, db_path, guild_id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("greeting watch tick failed for guild %s", guild_id)


async def greeting_watch_loop(bot: Bot, db_path: Path) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await run_tick(bot, db_path, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("greeting watch tick crashed")
        await asyncio.sleep(TICK_SECONDS)
