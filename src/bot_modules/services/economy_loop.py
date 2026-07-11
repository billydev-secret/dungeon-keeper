"""Nightly XP→currency conversion, driven by an hourly day-roll detector.

Each guild's local calendar day is tracked in ``econ_day_marks``. On the hour
we compare the current guild-local day to the stored mark; when it rolls
forward we sum the day-that-just-ended's ``xp_events`` per user and convert each
via :func:`economy_service.process_conversion` (idempotent per user/day), then
advance the mark **last** — so a crash mid-batch simply replays harmlessly on
the next tick. First sight of a guild only records the mark (no retroactive
conversion), and disabled guilds are skipped entirely.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy import logic
from bot_modules.services.economy_service import (
    load_econ_settings,
    member_is_booster,
    process_conversion,
)

log = logging.getLogger("dungeonkeeper.economy_loop")


def _seconds_until_next_hour() -> float:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (nxt - now).total_seconds()


def run_guild_day_roll(
    bot: discord.Client,
    conn: sqlite3.Connection,
    guild_id: int,
    now_ts: float,
) -> None:
    """Detect and process a guild-local day roll (idempotent per user/day).

    First sight of a guild just records the mark — nothing is converted
    retroactively. On a roll, every user with ``xp_events`` on the day that
    just ended (the stored mark's day) is converted with a booster ceil per
    member, then the mark advances to today **last**. Because
    ``process_conversion`` is idempotent, a crash before the mark update
    replays without double-crediting.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return

    offset = get_tz_offset_hours(conn, guild_id)
    today = logic.local_day_for(now_ts, offset)

    row = conn.execute(
        "SELECT last_local_day FROM econ_day_marks WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO econ_day_marks (guild_id, last_local_day) "
            "VALUES (?, ?)",
            (guild_id, today),
        )
        return

    last_day = row["last_local_day"]
    if last_day == today:
        return

    start, end = logic.local_day_bounds(last_day, offset)
    rows = conn.execute(
        """
        SELECT user_id, SUM(amount) AS xp
        FROM xp_events
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
        GROUP BY user_id
        """,
        (guild_id, start, end),
    ).fetchall()
    for r in rows:
        user_id = int(r["user_id"])
        xp = float(r["xp"] or 0.0)
        booster = member_is_booster(bot, guild_id, user_id)
        process_conversion(
            conn,
            settings,
            guild_id,
            user_id,
            local_day=last_day,
            xp=xp,
            booster=booster,
        )

    conn.execute(
        "UPDATE econ_day_marks SET last_local_day = ? WHERE guild_id = ?",
        (today, guild_id),
    )


async def economy_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        sleep_secs = _seconds_until_next_hour()
        await asyncio.sleep(sleep_secs)

        now_ts = time.time()
        for guild in list(bot.guilds):
            try:
                with open_db(db_path) as conn:
                    run_guild_day_roll(bot, conn, guild.id, now_ts)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: unhandled error for guild %s.", guild.id
                )
