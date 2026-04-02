"""Auto-delete service - manages scheduled message deletion in channels."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import discord

from db_utils import open_db
from settings import AUTO_DELETE_KEYWORDS, AUTO_DELETE_SETTINGS

GuildTextLike = discord.TextChannel | discord.Thread

log = logging.getLogger("dungeonkeeper.auto_delete")


def init_auto_delete_tables(conn: sqlite3.Connection) -> None:
    """Initialize database tables for auto-delete feature."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_delete_rules (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            max_age_seconds INTEGER NOT NULL,
            interval_seconds INTEGER NOT NULL,
            last_run_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_delete_messages (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id, message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_delete_messages_due
        ON auto_delete_messages (guild_id, channel_id, created_at)
        """
    )


def upsert_auto_delete_rule(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    max_age_seconds: int,
    interval_seconds: int,
    *,
    last_run_ts: float | None = None,
) -> None:
    """Create or update an auto-delete rule for a channel."""
    run_ts = time.time() if last_run_ts is None else last_run_ts
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO auto_delete_rules (
                guild_id,
                channel_id,
                max_age_seconds,
                interval_seconds,
                last_run_ts
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                max_age_seconds = excluded.max_age_seconds,
                interval_seconds = excluded.interval_seconds,
                last_run_ts = excluded.last_run_ts
            """,
            (guild_id, channel_id, max_age_seconds, interval_seconds, run_ts),
        )


def remove_auto_delete_rule(db_path: Path, guild_id: int, channel_id: int) -> bool:
    """Remove an auto-delete rule and all tracked messages for a channel."""
    with open_db(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM auto_delete_rules WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        conn.execute(
            "DELETE FROM auto_delete_messages WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        return cursor.rowcount > 0


def touch_auto_delete_rule_run(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    run_ts: float,
) -> None:
    """Update the last run timestamp for an auto-delete rule."""
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE auto_delete_rules SET last_run_ts = ? WHERE guild_id = ? AND channel_id = ?",
            (run_ts, guild_id, channel_id),
        )


def list_auto_delete_rules(db_path: Path) -> list[sqlite3.Row]:
    """List all auto-delete rules."""
    with open_db(db_path) as conn:
        return conn.execute(
            """
            SELECT guild_id, channel_id, max_age_seconds, interval_seconds, last_run_ts
            FROM auto_delete_rules
            ORDER BY guild_id, channel_id
            """
        ).fetchall()


def list_auto_delete_rules_for_guild(
    db_path: Path,
    guild_id: int,
) -> list[sqlite3.Row]:
    """List auto-delete rules for a specific guild."""
    with open_db(db_path) as conn:
        return conn.execute(
            """
            SELECT guild_id, channel_id, max_age_seconds, interval_seconds, last_run_ts
            FROM auto_delete_rules
            WHERE guild_id = ?
            ORDER BY channel_id
            """,
            (guild_id,),
        ).fetchall()


def auto_delete_rule_exists(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
) -> bool:
    """Check if an auto-delete rule exists for a channel."""
    row = conn.execute(
        """
        SELECT 1
        FROM auto_delete_rules
        WHERE guild_id = ? AND channel_id = ?
        LIMIT 1
        """,
        (guild_id, channel_id),
    ).fetchone()
    return row is not None


def track_auto_delete_message(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    created_at: float,
) -> None:
    """Track a message for potential auto-deletion."""
    conn.execute(
        """
        INSERT OR IGNORE INTO auto_delete_messages (guild_id, channel_id, message_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, channel_id, message_id, created_at),
    )


def remove_tracked_auto_delete_message(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    """Remove a tracked message from the auto-delete queue."""
    with open_db(db_path) as conn:
        conn.execute(
            """
            DELETE FROM auto_delete_messages
            WHERE guild_id = ? AND channel_id = ? AND message_id = ?
            """,
            (guild_id, channel_id, message_id),
        )


def remove_tracked_auto_delete_messages(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    message_ids: set[int],
) -> None:
    """Remove multiple tracked messages from the auto-delete queue."""
    if not message_ids:
        return
    with open_db(db_path) as conn:
        conn.executemany(
            """
            DELETE FROM auto_delete_messages
            WHERE guild_id = ? AND channel_id = ? AND message_id = ?
            """,
            [(guild_id, channel_id, message_id) for message_id in message_ids],
        )


_BULK_DELETE_MAX_AGE = 13 * 24 * 3600  # 13-day buffer before Discord's hard 14-day cutoff
_BULK_CHUNK = 100


def pop_due_auto_delete_message_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    cutoff_ts: float,
    *,
    limit: int = 500,
) -> list[tuple[int, float]]:
    """Get (message_id, created_at) pairs that are due for deletion."""
    rows = conn.execute(
        """
        SELECT message_id, created_at
        FROM auto_delete_messages
        WHERE guild_id = ? AND channel_id = ? AND created_at <= ?
        ORDER BY created_at, message_id
        LIMIT ?
        """,
        (guild_id, channel_id, cutoff_ts, limit),
    ).fetchall()
    return [(int(row["message_id"]), float(row["created_at"])) for row in rows]


async def delete_tracked_messages_older_than(
    db_path: Path,
    guild_id: int,
    channel: GuildTextLike,
    cutoff_ts: float,
    *,
    reason: str,
) -> tuple[int, int, int]:
    """
    Delete tracked messages older than cutoff timestamp.

    Uses bulk delete (up to 100 per request) for messages < 13 days old,
    and individual deletes for older messages. Returns (queued, deleted, failed).
    """
    with open_db(db_path) as conn:
        due = pop_due_auto_delete_message_ids(conn, guild_id, channel.id, cutoff_ts)

    if not due:
        return 0, 0, 0

    queued = len(due)
    deleted = 0
    failed = 0
    now = time.time()
    bulk_cutoff = now - _BULK_DELETE_MAX_AGE

    bulk = [(mid, ts) for mid, ts in due if ts > bulk_cutoff]
    old = [(mid, ts) for mid, ts in due if ts <= bulk_cutoff]

    # Bulk-delete recent messages in chunks of 100
    for i in range(0, len(bulk), _BULK_CHUNK):
        chunk_ids = [mid for mid, _ in bulk[i:i + _BULK_CHUNK]]
        partials = [channel.get_partial_message(mid) for mid in chunk_ids]
        try:
            await channel.delete_messages(partials, reason=reason)
            deleted += len(chunk_ids)
            remove_tracked_auto_delete_messages(db_path, guild_id, channel.id, set(chunk_ids))
        except discord.Forbidden:
            failed += len(chunk_ids)
            return queued, deleted, failed
        except discord.HTTPException:
            failed += len(chunk_ids)

        if i + _BULK_CHUNK < len(bulk):
            await asyncio.sleep(AUTO_DELETE_SETTINGS.bulk_delete_pause_seconds)

    # Individual delete for messages older than 13 days
    next_delete_at = 0.0
    for mid, _ in old:
        now_monotonic = time.monotonic()
        if now_monotonic < next_delete_at:
            await asyncio.sleep(next_delete_at - now_monotonic)

        partial = channel.get_partial_message(mid)
        try:
            delete_call = cast(Any, partial.delete)
            try:
                await delete_call(reason=reason)
            except TypeError:
                await partial.delete()
            deleted += 1
            remove_tracked_auto_delete_message(db_path, guild_id, channel.id, mid)
            next_delete_at = time.monotonic() + AUTO_DELETE_SETTINGS.delete_pause_seconds
        except discord.NotFound:
            remove_tracked_auto_delete_message(db_path, guild_id, channel.id, mid)
        except discord.Forbidden:
            failed += 1
            break
        except discord.HTTPException:
            failed += 1

    return queued, deleted, failed


def format_duration_seconds(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    if seconds <= 0:
        return "0s"
    units = (
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
    )
    for unit_seconds, unit_label in units:
        if seconds % unit_seconds == 0:
            amount = seconds // unit_seconds
            suffix = "" if amount == 1 else "s"
            return f"{amount} {unit_label}{suffix}"
    return f"{seconds} seconds"


def parse_duration_seconds(value: str) -> int | None:
    """Parse duration string into seconds."""
    text = value.strip().lower()
    if not text:
        return None
    if text in AUTO_DELETE_KEYWORDS.named_intervals:
        return AUTO_DELETE_KEYWORDS.named_intervals[text]

    total = 0
    cursor = 0
    for match in AUTO_DELETE_KEYWORDS.duration_pattern.finditer(text):
        separator = text[cursor : match.start()]
        if separator.strip():
            return None

        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("w"):
            multiplier = 7 * 24 * 60 * 60
        elif unit.startswith("d"):
            multiplier = 24 * 60 * 60
        elif unit.startswith("h"):
            multiplier = 60 * 60
        elif unit.startswith("m"):
            multiplier = 60
        else:
            multiplier = 1
        total += amount * multiplier
        cursor = match.end()

    if cursor == 0:
        return None

    if text[cursor:].strip():
        return None

    return total if total > 0 else None


async def process_auto_delete_tick(
    bot: discord.Client,
    db_path: Path,
) -> None:
    """Process one auto-delete tick, deleting messages from rules that are due."""
    now_ts = time.time()
    rules = list_auto_delete_rules(db_path)
    if not rules:
        return

    for rule in rules:
        guild_id = int(rule["guild_id"])
        channel_id = int(rule["channel_id"])
        max_age_seconds = int(rule["max_age_seconds"])
        interval_seconds = int(rule["interval_seconds"])
        last_run_ts = float(rule["last_run_ts"])

        if now_ts - last_run_ts < interval_seconds:
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        from utils import get_guild_channel_or_thread

        channel = get_guild_channel_or_thread(guild, channel_id)
        if channel is None:
            log.warning(
                "Auto-delete channel %s not found in guild %s; skipping rule.",
                channel_id,
                guild_id,
            )
            continue

        cutoff_ts = now_ts - max_age_seconds
        try:
            queued, deleted, failed = await delete_tracked_messages_older_than(
                db_path,
                guild_id,
                channel,
                cutoff_ts,
                reason="Auto-delete scheduled cleanup",
            )
            if queued > 0:
                log.info(
                    "Auto-delete in #%s (%s): queued=%s deleted=%s failed=%s",
                    channel.name,
                    guild.name,
                    queued,
                    deleted,
                    failed,
                )
            touch_auto_delete_rule_run(db_path, guild_id, channel_id, now_ts)
        except Exception:
            log.exception(
                "Auto-delete tick failed for guild=%s channel=#%s",
                guild.name,
                channel.name,
            )


async def _scan_and_delete_channel_history(
    channel: GuildTextLike,
    cutoff: datetime,
    *,
    reason: str,
) -> tuple[int, int]:
    """Scan channel history and delete unpinned messages older than cutoff datetime.

    Uses bulk delete (up to 100 per request) for messages < 13 days old,
    and individual deletes for older messages.
    """
    deleted = 0
    failed = 0
    bulk_cutoff_ts = time.time() - _BULK_DELETE_MAX_AGE

    bulk_batch: list[discord.PartialMessage] = []
    old_batch: list[discord.PartialMessage] = []

    async def _flush_bulk() -> bool:
        nonlocal deleted, failed
        if not bulk_batch:
            return True
        chunk = bulk_batch[:]
        bulk_batch.clear()
        try:
            await channel.delete_messages(chunk, reason=reason)
            deleted += len(chunk)
        except discord.Forbidden:
            failed += len(chunk)
            return False
        except discord.HTTPException:
            failed += len(chunk)
        await asyncio.sleep(AUTO_DELETE_SETTINGS.bulk_delete_pause_seconds)
        return True

    async for message in channel.history(limit=None, before=cutoff, oldest_first=True):
        if message.pinned:
            continue

        msg_ts = message.created_at.timestamp() if message.created_at else 0.0
        if msg_ts > bulk_cutoff_ts:
            bulk_batch.append(channel.get_partial_message(message.id))
            if len(bulk_batch) >= _BULK_CHUNK:
                if not await _flush_bulk():
                    return deleted, failed
        else:
            old_batch.append(channel.get_partial_message(message.id))

    # Flush any remaining bulk messages
    await _flush_bulk()

    # Individual delete for messages older than 13 days
    next_delete_at = 0.0
    for partial in old_batch:
        now_monotonic = time.monotonic()
        if now_monotonic < next_delete_at:
            await asyncio.sleep(next_delete_at - now_monotonic)
        try:
            delete_call = cast(Any, partial.delete)
            try:
                await delete_call(reason=reason)
            except TypeError:
                await partial.delete()
            deleted += 1
            next_delete_at = time.monotonic() + AUTO_DELETE_SETTINGS.delete_pause_seconds
        except discord.NotFound:
            pass
        except discord.Forbidden:
            failed += 1
            break
        except discord.HTTPException:
            failed += 1

    return deleted, failed


async def run_startup_auto_delete(bot: discord.Client, db_path: Path) -> None:
    """On startup, scan every auto-delete channel for messages that should already be gone."""
    from datetime import timedelta

    from utils import get_guild_channel_or_thread

    rules = list_auto_delete_rules(db_path)
    if not rules:
        return

    now_ts = time.time()
    for rule in rules:
        guild_id = int(rule["guild_id"])
        channel_id = int(rule["channel_id"])
        max_age_seconds = int(rule["max_age_seconds"])
        interval_seconds = int(rule["interval_seconds"])
        last_run_ts = float(rule["last_run_ts"])

        # Skip if the regular loop ran recently enough that nothing new is due
        if now_ts - last_run_ts < interval_seconds:
            log.debug(
                "Auto-delete startup: skipping channel %s (last run %.0fs ago, interval %ss)",
                channel_id,
                now_ts - last_run_ts,
                interval_seconds,
            )
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        channel = get_guild_channel_or_thread(guild, channel_id)
        if channel is None:
            log.warning(
                "Auto-delete startup: channel %s not found in guild %s; skipping.",
                channel_id,
                guild_id,
            )
            continue

        cutoff = discord.utils.utcnow() - timedelta(seconds=max_age_seconds)
        try:
            deleted, failed = await _scan_and_delete_channel_history(
                channel, cutoff, reason="Auto-delete startup catchup"
            )
            if deleted > 0 or failed > 0:
                log.info(
                    "Auto-delete startup #%s (%s): deleted=%s failed=%s",
                    channel.name,
                    guild.name,
                    deleted,
                    failed,
                )
            touch_auto_delete_rule_run(db_path, guild_id, channel_id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Auto-delete startup failed for guild=%s channel=#%s",
                guild.name,
                channel.name,
            )

        # Pause between channels so delete buckets from the previous channel
        # can settle before we start fetching and deleting in the next one.
        await asyncio.sleep(AUTO_DELETE_SETTINGS.bulk_delete_pause_seconds)


async def auto_delete_loop(bot: discord.Client, db_path: Path) -> None:
    """Background task that periodically processes auto-delete rules."""
    await bot.wait_until_ready()

    await run_startup_auto_delete(bot, db_path)

    while not bot.is_closed():
        try:
            await process_auto_delete_tick(bot, db_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Auto-delete loop iteration failed.")

        await asyncio.sleep(AUTO_DELETE_SETTINGS.poll_seconds)
