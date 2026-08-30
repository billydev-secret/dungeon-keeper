"""Auto-delete service - manages scheduled message deletion in channels."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.core.settings import AUTO_DELETE_SETTINGS
from bot_modules.core.utils import format_guild_for_log, jump_url
from bot_modules.services.message_store import (
    DELETE_SOURCE_AUTO_DELETE,
    clear_deleted_flag,
    mark_messages_deleted,
)

GuildTextLike = discord.TextChannel | discord.Thread


log = logging.getLogger("dungeonkeeper.auto_delete")


def _claim_deleted(
    db_path: Path | None, guild_id: int | None, message_ids: set[int]
) -> None:
    """Flag messages as auto-deleted *before* asking Discord to delete them.

    Order matters. The gateway's delete event arrives moments later carrying no
    actor, and ``mark_messages_deleted`` is first-writer-wins — so claiming
    beforehand is the only way this sweep's messages end up attributed to
    ``auto_delete`` rather than to the generic ``discord`` source. Paired with
    :func:`_release_deleted` for the case where the delete doesn't happen.

    ``db_path``/``guild_id`` are optional because the history scan is also
    called as a plain channel sweep with neither; marking is then skipped here
    rather than at every call site.
    """
    if db_path is None or guild_id is None or not message_ids:
        return
    try:
        with open_db(db_path) as conn:
            mark_messages_deleted(
                conn,
                guild_id,
                message_ids,
                DELETE_SOURCE_AUTO_DELETE,
                int(time.time()),
            )
    except Exception:
        # Bookkeeping must never abort a sweep — a missing badge is a far
        # smaller problem than messages that stop being deleted.
        log.exception("auto-delete: failed to claim deletion marks")


def _release_deleted(
    db_path: Path | None, guild_id: int | None, message_ids: set[int]
) -> None:
    """Roll back a claim for messages Discord refused to delete."""
    if db_path is None or guild_id is None or not message_ids:
        return
    try:
        with open_db(db_path) as conn:
            clear_deleted_flag(conn, guild_id, message_ids, DELETE_SOURCE_AUTO_DELETE)
    except Exception:
        log.exception("auto-delete: failed to release deletion marks")




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
            media_only INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )
    # Migration: add media_only to rule tables created before the media-only mode.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(auto_delete_rules)").fetchall()}
    if "media_only" not in cols:
        conn.execute(
            "ALTER TABLE auto_delete_rules ADD COLUMN media_only INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_delete_messages (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id, message_id)
        )
        """
    )
    # Migration: retry bookkeeping for queues built before bounded retry.
    # Both default to 0, which reads as "never failed, due now".
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(auto_delete_messages)")}
    if "attempts" not in msg_cols:
        conn.execute(
            "ALTER TABLE auto_delete_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "next_attempt_ts" not in msg_cols:
        conn.execute(
            "ALTER TABLE auto_delete_messages "
            "ADD COLUMN next_attempt_ts REAL NOT NULL DEFAULT 0"
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
    media_only: bool = False,
    last_run_ts: float | None = None,
) -> None:
    """Create or update an auto-delete rule for a channel.

    When ``media_only`` is toggled on an existing rule, the channel's tracked
    message queue is cleared: the sweep is queue-driven and can't re-inspect a
    message's attachments at delete time, so a queue built under the old mode
    could delete messages that no longer match. Clearing only un-tracks them
    (nothing is deleted); the next startup catch-up rebuilds the queue under the
    new mode. Editing age/interval without changing the mode leaves the queue
    intact.
    """
    run_ts = time.time() if last_run_ts is None else last_run_ts
    with open_db(db_path) as conn:
        prev = conn.execute(
            "SELECT media_only FROM auto_delete_rules WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO auto_delete_rules (
                guild_id,
                channel_id,
                max_age_seconds,
                interval_seconds,
                last_run_ts,
                media_only
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                max_age_seconds = excluded.max_age_seconds,
                interval_seconds = excluded.interval_seconds,
                last_run_ts = excluded.last_run_ts,
                media_only = excluded.media_only
            """,
            (guild_id, channel_id, max_age_seconds, interval_seconds, run_ts, int(media_only)),
        )
        if prev is not None and bool(prev["media_only"]) != bool(media_only):
            conn.execute(
                "DELETE FROM auto_delete_messages WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
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
            SELECT guild_id, channel_id, max_age_seconds, interval_seconds,
                   last_run_ts, media_only
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
        SELECT guild_id, channel_id, max_age_seconds, interval_seconds,
               last_run_ts, media_only
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


def should_track_auto_delete_message(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    has_media: bool,
) -> bool:
    """Return True if a message in this channel should be queued for auto-delete.

    False when no rule covers the channel. A ``media_only`` rule queues only
    messages carrying an attachment; a regular rule queues everything. This is
    the single gate for every queue-insertion site — the sweep is queue-driven,
    so filtering here is what makes media-only deletion correct.
    """
    row = conn.execute(
        """
        SELECT media_only
        FROM auto_delete_rules
        WHERE guild_id = ? AND channel_id = ?
        LIMIT 1
        """,
        (guild_id, channel_id),
    ).fetchone()
    if row is None:
        return False
    return has_media or not bool(row["media_only"])


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

# Bounded retry for deletes Discord refuses with a transient error.
#
# A failure used to untrack the message outright ("avoid infinite retry"), which
# made it a permanent orphan: the sweep is queue-driven, and the bounded startup
# scan can't reach back past ``last_run_ts - max_age`` to find it again. That
# cost three messages in #flash-channel on 2026-08-13. Now a failure costs one
# attempt and parks the row until the next backoff step, so the rest of the
# queue drains around it and a transient 429/500 fixes itself. These are the
# waits *between* tries, so the message is attempted MAX_DELETE_ATTEMPTS times
# across ~7.4 hours before the sweep abandons it — loudly, and without
# discarding the row.
RETRY_BACKOFF_SECONDS = (60, 300, 900, 3600, 21600)  # 1m, 5m, 15m, 1h, 6h
MAX_DELETE_ATTEMPTS = len(RETRY_BACKOFF_SECONDS) + 1


class AutoDeleteAbandoned(RuntimeError):
    """A message that exhausted its retry budget and will never be swept.

    Raised and caught at the point of failure purely so the give-up lands in the
    log as an ``ERROR`` with a traceback chaining the underlying Discord error.
    It deliberately does not escape the sweep: one cursed message must not stop
    the other due messages in the channel from being deleted.
    """


def record_auto_delete_failure(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    message_ids: set[int],
    *,
    now_ts: float | None = None,
) -> list[int]:
    """Charge a failed delete against each message's retry budget.

    Returns the ids that just ran out of attempts. Those rows are **kept** —
    ``attempts >= MAX_DELETE_ATTEMPTS`` filters them out of the due query, so
    nothing retries them, but the queue still records what is stuck. Deleting
    the row instead is what orphaned messages in the first place, and the row
    clears itself when the message is eventually deleted (the gateway delete
    listener untracks it).
    """
    if not message_ids:
        return []
    now = time.time() if now_ts is None else now_ts
    abandoned: list[int] = []
    with open_db(db_path) as conn:
        for message_id in sorted(message_ids):
            row = conn.execute(
                """
                SELECT attempts FROM auto_delete_messages
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                """,
                (guild_id, channel_id, message_id),
            ).fetchone()
            if row is None:
                # Raced with a delete event that untracked it — nothing to charge.
                continue
            attempts = int(row["attempts"]) + 1
            if attempts >= MAX_DELETE_ATTEMPTS:
                next_ts = now
                abandoned.append(message_id)
            else:
                next_ts = now + RETRY_BACKOFF_SECONDS[attempts - 1]
            conn.execute(
                """
                UPDATE auto_delete_messages
                SET attempts = ?, next_attempt_ts = ?
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                """,
                (attempts, next_ts, guild_id, channel_id, message_id),
            )
    return abandoned


def queue_auto_delete_messages(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    messages: list[tuple[int, float]],
) -> None:
    """Add (message_id, created_at) pairs to the queue, ignoring duplicates."""
    if not messages:
        return
    with open_db(db_path) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO auto_delete_messages
                (guild_id, channel_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [(guild_id, channel_id, mid, created_at) for mid, created_at in messages],
        )


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


# Old enough to delete, not parked by a backoff, and still inside its retry
# budget. Every "is it due?" question uses this, so the count that drives the
# progress log can't disagree with the rows the sweep actually pulls.
_DUE_PREDICATE = """
    guild_id = ? AND channel_id = ? AND created_at <= ?
    AND next_attempt_ts <= ? AND attempts < ?
"""


def count_due_auto_delete_messages(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    cutoff_ts: float,
    *,
    now_ts: float | None = None,
) -> int:
    """Count the messages a sweep would attempt right now."""
    now = time.time() if now_ts is None else now_ts
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM auto_delete_messages WHERE {_DUE_PREDICATE}",
        (guild_id, channel_id, cutoff_ts, now, MAX_DELETE_ATTEMPTS),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def pop_due_auto_delete_message_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    cutoff_ts: float,
    *,
    now_ts: float | None = None,
    limit: int = 500,
) -> list[tuple[int, float]]:
    """Get (message_id, created_at) pairs that are due for deletion."""
    now = time.time() if now_ts is None else now_ts
    rows = conn.execute(
        f"""
        SELECT message_id, created_at
        FROM auto_delete_messages
        WHERE {_DUE_PREDICATE}
        ORDER BY created_at, message_id
        LIMIT ?
        """,
        (guild_id, channel_id, cutoff_ts, now, MAX_DELETE_ATTEMPTS, limit),
    ).fetchall()
    return [(int(row["message_id"]), float(row["created_at"])) for row in rows]


def _describe_http_error(exc: discord.HTTPException) -> str:
    """HTTP status + Discord error code — the two facts the old bare except ate."""
    return f"HTTP {exc.status}, discord code {getattr(exc, 'code', None)}"


async def _dm_operator_about_abandoned(
    bot: discord.Client | None,
    channel: GuildTextLike,
    message_id: int,
    exc: discord.HTTPException,
) -> None:
    """DM the operator about one abandoned message.

    Targets ``SUPPORT_USER_ID`` — the same operator the watchdog pages — rather
    than ``guild.owner``, because rules exist in guilds the operator doesn't own
    and this is bot-internal news, not a moderation notice.
    """
    raw = os.getenv("SUPPORT_USER_ID", "").strip()
    if bot is None or not raw.isdigit():
        return
    channel_name = getattr(channel, "name", str(channel.id))
    guild = getattr(channel, "guild", None)
    link = jump_url(getattr(guild, "id", "@me"), channel.id, message_id)
    text = (
        f"🛑 **Auto-delete gave up** on a message in #{channel_name} after "
        f"{MAX_DELETE_ATTEMPTS} attempts ({_describe_http_error(exc)}).\n"
        f"It will not be retried — delete it by hand if it should be gone.\n"
        f"`{message_id}` — {link}"
    )
    try:
        user = bot.get_user(int(raw)) or await bot.fetch_user(int(raw))
        if user is not None:
            await user.send(text)
    except discord.HTTPException as dm_exc:  # Forbidden is a subclass
        log.warning(
            "Auto-delete: could not DM the operator about abandoned message %s (%s)",
            message_id,
            dm_exc,
        )


async def _charge_delete_failure(
    db_path: Path,
    guild_id: int,
    channel: GuildTextLike,
    message_ids: set[int],
    exc: discord.HTTPException,
    *,
    now_ts: float,
    bot: discord.Client | None,
) -> None:
    """Log a failed delete, charge the retry budget, and report give-ups."""
    channel_name = getattr(channel, "name", str(channel.id))
    log.warning(
        "Auto-delete #%s: delete failed for %s message(s) (%s): %s",
        channel_name,
        len(message_ids),
        _describe_http_error(exc),
        exc.text,
    )
    abandoned = record_auto_delete_failure(
        db_path, guild_id, channel.id, message_ids, now_ts=now_ts
    )
    for message_id in abandoned:
        try:
            raise AutoDeleteAbandoned(
                f"message {message_id} in #{channel_name}"
            ) from exc
        except AutoDeleteAbandoned:
            log.exception(
                "Auto-delete #%s: gave up on message %s after %s attempts (last error: %s)",
                channel_name,
                message_id,
                MAX_DELETE_ATTEMPTS,
                _describe_http_error(exc),
            )
        await _dm_operator_about_abandoned(bot, channel, message_id, exc)


async def delete_tracked_messages_older_than(
    db_path: Path,
    guild_id: int,
    channel: GuildTextLike,
    cutoff_ts: float,
    *,
    reason: str,
    now_ts: float | None = None,
    bot: discord.Client | None = None,
) -> tuple[int, int, int]:
    """
    Delete tracked messages older than cutoff timestamp.

    Uses bulk delete (up to 100 per request) for messages < 13 days old,
    and individual deletes for older messages.  Loops until all due messages
    are processed so a backlog is drained in one call instead of waiting for
    the next tick.  Returns (queued, deleted, failed).

    A delete Discord refuses with a transient error costs the message an
    attempt and parks it behind a backoff (see ``RETRY_BACKOFF_SECONDS``); the
    drain loop therefore skips it for the rest of this call and moves on to the
    rest of the queue. ``bot`` is only needed to DM the operator when a message
    exhausts its budget; ``now_ts`` pins the clock for the whole sweep and
    exists mainly so tests can drive the backoff schedule.
    """
    now = time.time() if now_ts is None else now_ts

    # Count total due upfront so progress logs can show "X / total"
    with open_db(db_path) as conn:
        grand_total = count_due_auto_delete_messages(
            conn, guild_id, channel.id, cutoff_ts, now_ts=now
        )

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
            due = pop_due_auto_delete_message_ids(
                conn, guild_id, channel.id, cutoff_ts, now_ts=now
            )

        if not due:
            break

        bulk_ids, old_ids = partition_messages_by_age(due, now)

        # Bulk-delete recent messages in chunks of 100
        abort = False
        for i in range(0, len(bulk_ids), _BULK_CHUNK):
            chunk_ids = bulk_ids[i : i + _BULK_CHUNK]
            chunk_set = set(chunk_ids)
            partials = [channel.get_partial_message(mid) for mid in chunk_ids]
            _claim_deleted(db_path, guild_id, chunk_set)
            try:
                await channel.delete_messages(partials, reason=reason)
                total_deleted += len(chunk_ids)
                remove_tracked_auto_delete_messages(
                    db_path, guild_id, channel.id, set(chunk_ids)
                )
            except discord.Forbidden:
                _release_deleted(db_path, guild_id, chunk_set)
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
            except discord.NotFound:
                # A 404 names the request, not which id in it is stale. For a
                # single-message chunk (discord.py degrades those to a plain
                # delete) that's unambiguous — the message is gone, so the claim
                # stands and it leaves the queue. For a real chunk, retry the
                # ids one at a time below, where a 404 can be pinned on the
                # message that earned it instead of condemning its neighbours.
                if len(chunk_ids) == 1:
                    remove_tracked_auto_delete_messages(
                        db_path, guild_id, channel.id, chunk_set
                    )
                else:
                    old_ids.extend(chunk_ids)
            except discord.HTTPException as exc:
                _release_deleted(db_path, guild_id, chunk_set)
                total_failed += len(chunk_ids)
                await _charge_delete_failure(
                    db_path, guild_id, channel, chunk_set, exc, now_ts=now, bot=bot
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
            _claim_deleted(db_path, guild_id, {mid})
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
                # Already gone — the claim stands, it just wasn't us who did it.
                remove_tracked_auto_delete_message(db_path, guild_id, channel.id, mid)
            except discord.Forbidden:
                # Channel-wide permission gap, not a verdict on this message —
                # it doesn't consume the retry budget, and the sweep stops here
                # rather than burning attempts on every other queued message.
                _release_deleted(db_path, guild_id, {mid})
                total_failed += 1
                abort = True
                break
            except discord.HTTPException as exc:
                _release_deleted(db_path, guild_id, {mid})
                total_failed += 1
                await _charge_delete_failure(
                    db_path, guild_id, channel, {mid}, exc, now_ts=now, bot=bot
                )

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

        from bot_modules.core.utils import get_guild_channel_or_thread

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
                now_ts=now_ts,
                bot=bot,
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
    media_only: bool = False,
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

    def _hand_failures_to_the_queue(message_ids: set[int]) -> None:
        """Queue messages this scan failed to delete so the tick can retry them.

        The scan walks history, not the queue, so these messages may not be
        tracked at all — dropping them here is exactly how a message becomes an
        orphan the next bounded scan can't see. The tick owns the retry budget,
        so they go in with a clean one. ``created_at`` comes from the snowflake,
        which needs no extra API call.
        """
        if not track_messages:
            return
        assert db_path is not None
        assert guild_id is not None
        queue_auto_delete_messages(
            db_path,
            guild_id,
            channel.id,
            [
                (mid, discord.utils.snowflake_time(mid).timestamp())
                for mid in sorted(message_ids)
            ],
        )

    async def _flush_bulk() -> bool:
        nonlocal deleted, failed
        if not bulk_batch:
            return True
        chunk = bulk_batch[:]
        bulk_batch.clear()
        chunk_ids = {p.id for p in chunk}
        _claim_deleted(db_path, guild_id, chunk_ids)
        try:
            await channel.delete_messages(chunk, reason=reason)
            deleted += len(chunk)
        except discord.Forbidden:
            _release_deleted(db_path, guild_id, chunk_ids)
            failed += len(chunk)
            return False
        except discord.HTTPException as exc:
            _release_deleted(db_path, guild_id, chunk_ids)
            failed += len(chunk)
            log.warning(
                "Auto-delete scan #%s: bulk delete failed for %s message(s) (%s): %s",
                channel_name,
                len(chunk_ids),
                _describe_http_error(exc),
                exc.text,
            )
            _hand_failures_to_the_queue(chunk_ids)
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
        # media_only rules ignore text-only messages for both deletion and the
        # downtime-backfill below, mirroring the live queue-insertion gate.
        if media_only and not message.attachments:
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
        _claim_deleted(db_path, guild_id, {partial.id})
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
            pass  # Already gone — the claim stands.
        except discord.Forbidden:
            _release_deleted(db_path, guild_id, {partial.id})
            failed += 1
            break
        except discord.HTTPException as exc:
            _release_deleted(db_path, guild_id, {partial.id})
            failed += 1
            log.warning(
                "Auto-delete scan #%s: delete failed for message %s (%s): %s",
                channel_name,
                partial.id,
                _describe_http_error(exc),
                exc.text,
            )
            _hand_failures_to_the_queue({partial.id})
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

    from bot_modules.core.utils import get_guild_channel_or_thread

    guild_id = int(rule["guild_id"])
    channel_id = int(rule["channel_id"])
    max_age_seconds = int(rule["max_age_seconds"])
    interval_seconds = int(rule["interval_seconds"])
    last_run_ts = float(rule["last_run_ts"])
    media_only = bool(rule["media_only"])

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
                media_only=media_only,
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
