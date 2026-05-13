"""Auto-delete service - manages scheduled message deletion in channels."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import discord

from core.db_utils import open_db
from core.settings import AUTO_DELETE_KEYWORDS, AUTO_DELETE_SETTINGS
from core.utils import format_guild_for_log

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


def list_auto_delete_rules_for_guild_with_conn(
    conn: sqlite3.Connection,
    guild_id: int,
) -> list[sqlite3.Row]:
    """List auto-delete rules for a guild using an existing connection."""
    return conn.execute(
        """
        SELECT guild_id, channel_id, max_age_seconds, interval_seconds, last_run_ts
        FROM auto_delete_rules
        WHERE guild_id = ?
        ORDER BY channel_id
        """,
        (guild_id,),
    ).fetchall()


def list_auto_delete_rules_for_guild(
    db_path: Path,
    guild_id: int,
) -> list[sqlite3.Row]:
    """List auto-delete rules for a specific guild."""
    with open_db(db_path) as conn:
        return list_auto_delete_rules_for_guild_with_conn(conn, guild_id)


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


_BULK_DELETE_MAX_AGE = (
    13 * 24 * 3600
)  # 13-day buffer before Discord's hard 14-day cutoff
_BULK_CHUNK = 100


# ---------------------------------------------------------------------------
# Pure scheduling / partition decisions
# ---------------------------------------------------------------------------


def is_rule_due(now_ts: float, last_run_ts: float, interval_seconds: int) -> bool:
    """Return True if a rule's interval has elapsed since its last run.

    A rule that has never run (last_run_ts == 0) is always due. Uses strict `>=`
    so a rule with a 60-second interval can fire at exactly t+60.
    """
    return (now_ts - last_run_ts) >= interval_seconds


def compute_startup_scan_after(
    last_run_ts: float,
    max_age_seconds: int,
) -> datetime | None:
    """Return the lower-bound datetime for a bounded startup history scan.

    A previous run at ``last_run_ts`` already swept every message whose
    ``created_at`` was at most ``last_run_ts - max_age_seconds``, so on the
    next startup we only need to scan messages created after that bound.

    Returns ``None`` when the rule has never run, or when the bound would land
    at/before the unix epoch — both cases mean "scan the entire channel
    history", which is the only safe thing to do without prior-run state.
    """
    if last_run_ts <= 0:
        return None
    bound_ts = last_run_ts - max_age_seconds
    if bound_ts <= 0:
        return None
    return datetime.fromtimestamp(bound_ts, tz=timezone.utc)


def partition_messages_by_age(
    messages: list[tuple[int, float]],
    now_ts: float,
    bulk_age_limit: int = _BULK_DELETE_MAX_AGE,
) -> tuple[list[int], list[int]]:
    """Split (message_id, created_at) pairs into (bulk_eligible, individual_only).

    Discord's bulk-delete endpoint rejects messages older than 14 days; we use a
    13-day buffer. Messages at or under the threshold are bulk-eligible; older
    ones must be deleted one at a time.

    Returns two lists of message IDs, each preserving the input order.
    """
    bulk_cutoff_ts = now_ts - bulk_age_limit
    bulk: list[int] = []
    individual: list[int] = []
    for msg_id, created_at in messages:
        if created_at > bulk_cutoff_ts:
            bulk.append(msg_id)
        else:
            individual.append(msg_id)
    return bulk, individual


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
    and individual deletes for older messages.  Loops until all due messages
    are processed so a backlog is drained in one call instead of waiting for
    the next tick.  Returns (queued, deleted, failed).
    """
    # Count total due upfront so progress logs can show "X / total"
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM auto_delete_messages "
            "WHERE guild_id = ? AND channel_id = ? AND created_at <= ?",
            (guild_id, channel.id, cutoff_ts),
        ).fetchone()
        grand_total = int(row["cnt"]) if row else 0

    if grand_total == 0:
        return 0, 0, 0

    start_time = time.monotonic()
    total_deleted = 0
    total_failed = 0
    channel_name = getattr(channel, "name", str(channel.id))

    log.debug(
        "Auto-delete #%s: starting, %s messages due",
        channel_name,
        grand_total,
    )

    while True:
        with open_db(db_path) as conn:
            due = pop_due_auto_delete_message_ids(conn, guild_id, channel.id, cutoff_ts)

        if not due:
            break

        now = time.time()
        bulk_ids, old_ids = partition_messages_by_age(due, now)

        # Bulk-delete recent messages in chunks of 100
        abort = False
        for i in range(0, len(bulk_ids), _BULK_CHUNK):
            chunk_ids = bulk_ids[i : i + _BULK_CHUNK]
            partials = [channel.get_partial_message(mid) for mid in chunk_ids]
            try:
                await channel.delete_messages(partials, reason=reason)
                total_deleted += len(chunk_ids)
                remove_tracked_auto_delete_messages(
                    db_path, guild_id, channel.id, set(chunk_ids)
                )
            except discord.Forbidden:
                total_failed += len(chunk_ids)
                elapsed = time.monotonic() - start_time
                log.info(
                    "Auto-delete #%s: forbidden after %.1fs, %s/%s deleted, %s failed",
                    channel_name,
                    elapsed,
                    total_deleted,
                    grand_total,
                    total_failed,
                )
                return grand_total, total_deleted, total_failed
            except discord.HTTPException:
                total_failed += len(chunk_ids)
                # Remove from tracking to avoid infinite retry
                remove_tracked_auto_delete_messages(
                    db_path, guild_id, channel.id, set(chunk_ids)
                )

            if i + _BULK_CHUNK < len(bulk_ids):
                await asyncio.sleep(AUTO_DELETE_SETTINGS.bulk_delete_pause_seconds)

        # Individual delete for messages older than 13 days
        next_delete_at = 0.0
        for mid in old_ids:
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
                total_deleted += 1
                remove_tracked_auto_delete_message(db_path, guild_id, channel.id, mid)
                next_delete_at = (
                    time.monotonic() + AUTO_DELETE_SETTINGS.delete_pause_seconds
                )
            except discord.NotFound:
                remove_tracked_auto_delete_message(db_path, guild_id, channel.id, mid)
            except discord.Forbidden:
                total_failed += 1
                abort = True
                break
            except discord.HTTPException:
                total_failed += 1
                remove_tracked_auto_delete_message(db_path, guild_id, channel.id, mid)

        if abort:
            break

        log.debug(
            "Auto-delete #%s: %s/%s deleted (%.1fs elapsed)",
            channel_name,
            total_deleted,
            grand_total,
            time.monotonic() - start_time,
        )

    elapsed = time.monotonic() - start_time
    if total_failed > 0:
        log.info(
            "Auto-delete #%s: done in %.1fs, %s/%s deleted, %s failed",
            channel_name,
            elapsed,
            total_deleted,
            grand_total,
            total_failed,
        )
    else:
        log.debug(
            "Auto-delete #%s: done in %.1fs, %s/%s deleted",
            channel_name,
            elapsed,
            total_deleted,
            grand_total,
        )
    return grand_total, total_deleted, total_failed


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

        if not is_rule_due(now_ts, last_run_ts, interval_seconds):
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        from core.utils import get_guild_channel_or_thread

        channel = get_guild_channel_or_thread(guild, channel_id)
        if channel is None:
            log.warning(
                "Auto-delete channel %s not found in guild %s; skipping rule.",
                channel_id,
                format_guild_for_log(guild, guild_id),
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
            if failed > 0:
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
    after: datetime | None = None,
    db_path: Path | None = None,
    guild_id: int | None = None,
) -> tuple[int, int]:
    """Scan channel history and delete unpinned messages older than cutoff datetime.

    Uses bulk delete (up to 100 per request) for messages < 13 days old,
    and individual deletes for older messages. When ``after`` is set, the
    history walk skips anything created at or before that bound — used by
    the startup catch-up to avoid re-scanning history that a previous run
    already swept.

    When ``db_path`` and ``guild_id`` are both provided, the walk also reads
    past ``cutoff`` and inserts younger messages into ``auto_delete_messages``
    so the live tick path can age them out later. Without this, messages
    posted during bot downtime (when ``on_message`` doesn't fire) would
    become permanent orphans — invisible to the tick path forever.
    """
    track_messages = db_path is not None and guild_id is not None
    channel_name = getattr(channel, "name", str(channel.id))
    start_time = time.monotonic()
    deleted = 0
    failed = 0
    scanned = 0
    cutoff_ts = cutoff.timestamp()
    bulk_cutoff_ts = time.time() - _BULK_DELETE_MAX_AGE

    bulk_batch: list[discord.PartialMessage] = []
    old_batch: list[discord.PartialMessage] = []
    tracking_batch: list[tuple[int, float]] = []

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

    history_kwargs: dict[str, Any] = {
        "limit": None,
        "oldest_first": True,
    }
    if after is not None:
        history_kwargs["after"] = after
    # When tracking is off, cap the walk at `cutoff` (we only care about
    # already-eligible messages). When tracking is on we walk past cutoff
    # so we can pick up downtime-posted messages that aren't yet eligible.
    if not track_messages:
        history_kwargs["before"] = cutoff

    async for message in channel.history(**history_kwargs):
        if message.pinned:
            continue
        msg_ts = message.created_at.timestamp() if message.created_at else 0.0

        if track_messages and msg_ts > cutoff_ts:
            tracking_batch.append((message.id, msg_ts))
            continue

        scanned += 1
        if msg_ts > bulk_cutoff_ts:
            bulk_batch.append(channel.get_partial_message(message.id))
            if len(bulk_batch) >= _BULK_CHUNK:
                if not await _flush_bulk():
                    log.info(
                        "Auto-delete scan #%s: forbidden after %.1fs, %s/%s deleted, %s failed",
                        channel_name,
                        time.monotonic() - start_time,
                        deleted,
                        scanned,
                        failed,
                    )
                    return deleted, failed
        else:
            old_batch.append(channel.get_partial_message(message.id))

    if track_messages and tracking_batch:
        # db_path / guild_id are non-None when track_messages is True; the
        # asserts pin that for the type checker.
        assert db_path is not None
        assert guild_id is not None
        with open_db(db_path) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO auto_delete_messages
                    (guild_id, channel_id, message_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [(guild_id, channel.id, mid, ts) for mid, ts in tracking_batch],
            )
        log.debug(
            "Auto-delete scan #%s: tracked %s downtime messages",
            channel_name,
            len(tracking_batch),
        )

    if scanned == 0:
        return 0, 0

    log.debug(
        "Auto-delete scan #%s: starting, %s messages found (%s bulk, %s old)",
        channel_name,
        scanned,
        scanned - len(old_batch),
        len(old_batch),
    )

    # Flush any remaining bulk messages
    await _flush_bulk()

    # Individual delete for messages older than 13 days
    next_delete_at = 0.0
    old_processed = 0
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
            next_delete_at = (
                time.monotonic() + AUTO_DELETE_SETTINGS.delete_pause_seconds
            )
        except discord.NotFound:
            pass
        except discord.Forbidden:
            failed += 1
            break
        except discord.HTTPException:
            failed += 1
        old_processed += 1
        if old_processed % 50 == 0:
            log.debug(
                "Auto-delete scan #%s: %s/%s deleted (%.1fs elapsed)",
                channel_name,
                deleted,
                scanned,
                time.monotonic() - start_time,
            )

    elapsed = time.monotonic() - start_time
    if failed > 0:
        log.info(
            "Auto-delete scan #%s: done in %.1fs, %s/%s deleted, %s failed",
            channel_name,
            elapsed,
            deleted,
            scanned,
            failed,
        )
    else:
        log.debug(
            "Auto-delete scan #%s: done in %.1fs, %s/%s deleted",
            channel_name,
            elapsed,
            deleted,
            scanned,
        )
    return deleted, failed


async def _run_startup_for_rule(
    bot: discord.Client,
    db_path: Path,
    rule: sqlite3.Row,
    now_ts: float,
    semaphore: asyncio.Semaphore,
) -> None:
    """Run startup catch-up for a single auto-delete rule (held inside a semaphore)."""
    from datetime import timedelta

    from core.utils import get_guild_channel_or_thread

    guild_id = int(rule["guild_id"])
    channel_id = int(rule["channel_id"])
    max_age_seconds = int(rule["max_age_seconds"])
    interval_seconds = int(rule["interval_seconds"])
    last_run_ts = float(rule["last_run_ts"])

    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    channel = get_guild_channel_or_thread(guild, channel_id)
    if channel is None:
        log.warning(
            "Auto-delete startup: channel %s not found in guild %s; skipping.",
            channel_id,
            format_guild_for_log(guild, guild_id),
        )
        return

    cutoff = discord.utils.utcnow() - timedelta(seconds=max_age_seconds)
    after = compute_startup_scan_after(last_run_ts, max_age_seconds)

    async with semaphore:
        try:
            deleted, failed = await _scan_and_delete_channel_history(
                channel,
                cutoff,
                reason="Auto-delete startup catchup",
                after=after,
                db_path=db_path,
                guild_id=guild_id,
            )
            if failed > 0:
                log.info(
                    "Auto-delete startup #%s (%s): deleted=%s failed=%s",
                    channel.name,
                    guild.name,
                    deleted,
                    failed,
                )
            # Only advance the schedule if the rule was already overdue at boot.
            # Otherwise a restart would push the next regular tick out by a full
            # interval relative to its real schedule.
            if is_rule_due(now_ts, last_run_ts, interval_seconds):
                touch_auto_delete_rule_run(db_path, guild_id, channel_id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Auto-delete startup failed for guild=%s channel=#%s",
                guild.name,
                channel.name,
            )


async def run_startup_auto_delete(bot: discord.Client, db_path: Path) -> None:
    """On startup, scan every auto-delete channel for messages that should already be gone.

    Channels are processed in parallel (gated by ``startup_concurrency``) since
    Discord's rate-limit buckets are per-channel and don't conflict across channels.
    Each rule's history scan is bounded to the gap window since the previous run,
    so a frequently-restarted bot doesn't re-walk months of history every boot.
    """
    rules = list_auto_delete_rules(db_path)
    if not rules:
        return

    now_ts = time.time()
    semaphore = asyncio.Semaphore(AUTO_DELETE_SETTINGS.startup_concurrency)

    await asyncio.gather(
        *(_run_startup_for_rule(bot, db_path, rule, now_ts, semaphore) for rule in rules)
    )


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
