"""Daily aggregate of ``xp_events``, so the raw events can eventually be pruned.

Stage 1 of ``docs/plans/xp-events-retention-and-rollup.md``: build and maintain
``xp_daily``. Nothing reads it yet (Stage 2 teaches the six all-time readers to
union it) and nothing deletes raw events yet (Stage 3). Keeping those stages
apart is the point — the rollup gets to be checked against live data while the
raw rows it summarises are still sitting there to be checked against.

Everything here is **idempotent by rebuild**: a day is recomputed from raw
events and written wholesale, never incremented. That is what makes a re-run
after a crash, a backfill over a day that was already rolled up, and a repair
after a bug all the same operation. It is also the only correct choice for the
NULL-channel rows: ``channel_id`` is nullable and in the primary key, and
SQLite does not treat two NULLs as equal, so ``ON CONFLICT`` would silently
accumulate duplicate NULL-channel buckets instead of replacing them.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# A day is only rolled up once it can no longer receive events. Rolling up
# "today" would write a bucket that is still growing, and any reader that
# later trusted it would under-count. The sweep therefore stops at the start
# of the current UTC day.
_SECONDS_PER_DAY = 86400

# How much raw history stays queryable. This is one number in two roles: the
# horizon Stage 3 prunes behind, and the point where a unioning reader stops
# trusting raw and reads the rollup instead. They must be the same number —
# a reader that partitions later than the pruner deletes loses XP, and one
# that partitions earlier double-counts it.
RAW_RETENTION_DAYS = 90


def utc_day(ts: float) -> str:
    """The UTC 'YYYY-MM-DD' a timestamp falls in — the rollup's bucket key."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


_utc_day = utc_day  # internal alias, kept so the module reads uniformly


def _day_bounds(day: str) -> tuple[float, float]:
    """[start, end) unix bounds of a UTC 'YYYY-MM-DD'."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def current_utc_day(now: float | None = None) -> str:
    return _utc_day(time.time() if now is None else now)


def rollup_span(conn: sqlite3.Connection, first_day: str, last_day: str) -> int:
    """Rebuild every ``xp_daily`` bucket from ``first_day`` to ``last_day``.

    Inclusive at both ends, all guilds, in **one** pass over ``xp_events``.
    Returns the number of buckets written.

    Doing a span rather than a day at a time is not an optimisation detail, it
    is the difference between a backfill that finishes and one that does not.
    ``xp_events`` has no index on ``created_at`` alone — the only one that
    could serve a bare time range is ``(guild_id, created_at)``, and a rollup
    covering every guild has no ``guild_id`` predicate to lead with — so a
    per-day range scans the whole table. Over ~440 days of prod history that is
    ~540M row reads and the better part of an hour; grouping the day out of
    ``created_at`` instead makes it one scan. Measured on a snapshot of the
    live 965MB DB: 50+ minutes per-day, 24s as a span.

    Days inside the span with no events simply produce no rows, so a caller may
    pass a span containing gaps. Rebuilding a day that was already correct is
    the same operation as building it for the first time — see the module
    docstring on idempotence-by-rebuild.
    """
    start, _ = _day_bounds(first_day)
    _, end = _day_bounds(last_day)

    conn.execute(
        "DELETE FROM xp_daily WHERE day >= ? AND day <= ?", (first_day, last_day)
    )
    cur = conn.execute(
        """
        INSERT INTO xp_daily
            (guild_id, user_id, source, channel_id, day,
             xp, events, first_at, last_at)
        SELECT guild_id, user_id, source, channel_id,
               date(created_at, 'unixepoch') AS d,
               SUM(amount), COUNT(*), MIN(created_at), MAX(created_at)
        FROM xp_events
        WHERE created_at >= ? AND created_at < ?
        GROUP BY guild_id, user_id, source, channel_id, d
        """,
        (start, end),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def rollup_day(conn: sqlite3.Connection, day: str) -> int:
    """Rebuild every ``xp_daily`` bucket for one UTC day, across all guilds.

    A one-day :func:`rollup_span`. Deleting the day first is what lets a
    rebuild after the underlying events were themselves deleted (an erasure
    run, say) *shrink* the rollup rather than leave a stale total behind.
    """
    return rollup_span(conn, day, day)


def days_with_events(
    conn: sqlite3.Connection, *, before: str | None = None
) -> list[str]:
    """Every UTC day that has at least one raw event, oldest first.

    ``before`` excludes days on or after it — pass the current day so a
    still-growing bucket is never rolled up.
    """
    params: list[object] = []
    where = ""
    if before is not None:
        start, _ = _day_bounds(before)
        where = "WHERE created_at < ?"
        params.append(start)
    rows = conn.execute(
        f"""
        SELECT DISTINCT date(created_at, 'unixepoch') AS d
        FROM xp_events
        {where}
        ORDER BY d
        """,
        params,
    ).fetchall()
    return [str(r[0]) for r in rows]


def rolled_up_days(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0]) for r in conn.execute("SELECT DISTINCT day FROM xp_daily").fetchall()
    }


def rollup_pending_days(
    conn: sqlite3.Connection, *, now: float | None = None, limit: int | None = None
) -> tuple[int, int]:
    """Roll up every complete day that has events but no rollup yet.

    Returns ``(days_rolled, buckets_written)``. This is the daily job's body
    and the backfill both — a fresh install has ~180 pending days and a
    steady-state run has one, which is the same code path with a different
    count. ``limit`` caps a single run so the first backfill can be spread
    over several passes rather than blocking the loop on one long write.
    """
    today = current_utc_day(now)
    pending = [d for d in days_with_events(conn, before=today) if d not in rolled_up_days(conn)]
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        return 0, 0

    # One span covering the pending days, not one query each. Days in the gaps
    # are rebuilt too, which is free (they are already correct, and rebuilding
    # is the same operation) and avoids ~440 full scans of xp_events.
    buckets = rollup_span(conn, pending[0], pending[-1])
    log.info(
        "xp rollup: %d day(s) → %d bucket(s) (%s … %s)",
        len(pending), buckets, pending[0], pending[-1],
    )
    recompute_watermark(conn)
    return len(pending), buckets


def refresh_recent_days(
    conn: sqlite3.Connection, *, days: int = 2, now: float | None = None
) -> tuple[int, int]:
    """Recompute the last few complete days even if already rolled up.

    Late-arriving events are real: voice XP is credited when a session ends,
    and a backfill can insert events with old timestamps. A day rolled up at
    00:05 and then written to at 00:10 would be permanently wrong under
    rollup-once semantics, so the newest days are rebuilt on every pass. Cheap
    — a day is a few hundred buckets.
    """
    now_ts = time.time() if now is None else now
    today_start, _ = _day_bounds(current_utc_day(now_ts))

    targets = [
        _utc_day(today_start - i * _SECONDS_PER_DAY) for i in range(1, days + 1)
    ]
    buckets = rollup_span(conn, targets[-1], targets[0])
    recompute_watermark(conn)
    return len(targets), buckets


def recompute_watermark(conn: sqlite3.Connection) -> str | None:
    """Store the rollup's coverage: how far it reaches, and where it first breaks.

    ``rolled_through_day`` is the newest day D such that *every* day with raw
    events up to and including D has a rollup — a contiguous prefix, not
    merely the newest day present. A hole at day 5 holds it at day 4 even if
    days 6..180 are all rolled, because Stage 3 must never delete past it.

    ``first_gap_day`` is the oldest day that has events and no rollup, which
    is the different question the readers ask. Returns the watermark.
    """
    raw_days = days_with_events(conn, before=current_utc_day())
    done = rolled_up_days(conn)

    watermark: str | None = None
    gap: str | None = None
    for day in raw_days:  # days_with_events returns them oldest-first
        if day not in done:
            gap = day
            break
        watermark = day

    conn.execute(
        "UPDATE xp_rollup_state SET rolled_through_day = ?, first_gap_day = ?,"
        " updated_at = ? WHERE id = 1",
        (watermark, gap, time.time()),
    )
    return watermark


def get_watermark(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT rolled_through_day FROM xp_rollup_state WHERE id = 1"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def get_first_gap_day(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT first_gap_day FROM xp_rollup_state WHERE id = 1"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def read_boundary(
    conn: sqlite3.Connection, *, now: float | None = None
) -> tuple[str, float] | None:
    """Where a unioning reader should stop trusting raw events.

    Returns ``(boundary_day, boundary_ts)``: read ``xp_daily`` for
    ``day < boundary_day`` and ``xp_events`` for ``created_at >= boundary_ts``.
    ``None`` means read raw alone.

    The boundary is the **prune horizon**, not the rollup watermark. The
    watermark runs to yesterday, and partitioning there would push a 7-day
    leaderboard through the rollup — inheriting the rollup's day-granularity
    skew for a window whose raw events are all still present and exact. The
    rollup is only worth reading where raw data may be *missing*, which is
    precisely the range Stage 3 prunes.

    The watermark's job here is the safety check: if the rollup has not
    actually covered everything up to the boundary — the state during the
    first backfill, or after a hole — this returns ``None`` and the reader
    stays on raw, which is still complete because nothing has been pruned.
    """
    now_ts = time.time() if now is None else now
    boundary_day = _utc_day(now_ts - RAW_RETENTION_DAYS * _SECONDS_PER_DAY)

    # The test is "is any day below the boundary still unrolled", NOT "does
    # the watermark reach the boundary". A quiet guild whose newest event is
    # months old has a watermark far behind the boundary and a rollup that
    # nonetheless covers everything there is — comparing against the
    # watermark would refuse to read a complete rollup.
    gap = get_first_gap_day(conn)
    if gap is not None and gap < boundary_day:
        return None
    if get_watermark(conn) is None and gap is None:
        # Nothing rolled and nothing to roll: no history at all. Reading the
        # empty rollup is harmless, but so is raw — take raw, it is simpler.
        return None

    start, _ = _day_bounds(boundary_day)
    return boundary_day, start


def rollup_stats(
    conn: sqlite3.Connection, *, now: float | None = None
) -> dict[str, object]:
    """Coverage summary, for the log line and for eyeballing a backfill.

    ``days_missing`` excludes the current UTC day. Today is *deliberately*
    never rolled up — a still-growing bucket would freeze a partial total — so
    listing it as missing is a false alarm on every single run, and an operator
    reading a backfill's output needs the list to mean "something is wrong".
    """
    raw_days = set(days_with_events(conn, before=current_utc_day(now)))
    done = rolled_up_days(conn)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(events), 0), COALESCE(SUM(xp), 0.0) FROM xp_daily"
    ).fetchone()
    raw = conn.execute("SELECT COUNT(*) FROM xp_events").fetchone()
    return {
        "buckets": int(row[0]),
        "events_covered": int(row[1]),
        "xp_covered": float(row[2]),
        "raw_events": int(raw[0]),
        "days_with_events": len(raw_days),
        "days_rolled_up": len(done),
        "days_missing": sorted(raw_days - done),
    }


# ── Stage 3: retention ──────────────────────────────────────────────────
#
# Deleting a million rows of member activity is not a sweep like the others in
# the 2026-08 review, and the guards below are the reason. Every one of them
# fails *closed* — a prune that cannot prove the rollup covers what it is about
# to delete does nothing at all, because the raw events are the only copy and
# there is no undo.

# The config key that turns retention on for a guild. Off by default and
# deliberately per-guild: the rollup and the unioning readers are correct
# whether or not a guild has been pruned (the raw arm is filtered to the
# boundary either way), so this can be enabled on one guild, watched, and then
# rolled out — rather than being one switch that empties the table everywhere.
RETENTION_CONFIG_KEY = "xp_retention_enabled"

# Rows deleted per pass. The first prune on the busy guild has ~500k rows to
# clear; doing it in one statement would hold a write lock long enough to stall
# XP writes, so the loop nibbles and catches up over a few days.
PRUNE_CHUNK = 20_000


def retention_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    row = conn.execute(
        "SELECT value FROM config WHERE key = ? AND guild_id = ?",
        (RETENTION_CONFIG_KEY, guild_id),
    ).fetchone()
    return bool(row) and str(row[0]).strip() in ("1", "true", "True", "on", "yes")


def prunable_row_count(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> int:
    """How many raw rows a prune would delete for this guild, right now.

    The dry run. Answers the dashboard's "N events ready to be summarised"
    without touching anything, and returns 0 whenever a real prune would
    refuse — so the number shown is never larger than the number that would go.
    """
    boundary = read_boundary(conn, now=now)
    if boundary is None:
        return 0
    _, boundary_ts = boundary
    row = conn.execute(
        "SELECT COUNT(*) FROM xp_events WHERE guild_id = ? AND created_at < ?",
        (guild_id, boundary_ts),
    ).fetchone()
    return int(row[0]) if row else 0


class PruneRefused(Exception):
    """A guard said no. Carries the reason so the log line is useful."""


def prune_raw_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    limit: int = PRUNE_CHUNK,
) -> int:
    """Delete raw ``xp_events`` the rollup already covers. Returns rows deleted.

    Stage 3 of docs/plans/xp-events-retention-and-rollup.md — the only code in
    this feature that destroys anything. Raises ``PruneRefused`` rather than
    deleting a partial or unverified range.

    The guards, in the order they run:

    1. **The guild has opted in.** ``RETENTION_CONFIG_KEY`` is off by default.
    2. **The rollup is readable at all.** ``read_boundary`` returns ``None``
       while any day below the boundary is unrolled — mid-backfill, or after a
       hole — and that is exactly when deleting would lose XP the readers can
       no longer find.
    3. **The contiguous watermark reaches the boundary.** ``read_boundary``
       checks for a *gap*; this checks that the rolled prefix actually extends
       to the last day being deleted. A rollup that has never run has no gap
       either, and the two questions differ.
    4. **Every day in the range being deleted has rollup rows**, checked
       against the range itself rather than trusting the watermark. Days with
       no events at all are fine — a day is only required to be present in
       ``xp_daily`` if it is present in ``xp_events``.

    Only then does it delete, oldest first, at most ``limit`` rows — so a first
    pass over half a million rows is spread across days instead of held in one
    write. Nothing is deleted at or above the boundary, ever.
    """
    if not retention_enabled(conn, guild_id):
        raise PruneRefused("retention is not enabled for this guild")

    boundary = read_boundary(conn, now=now)
    if boundary is None:
        raise PruneRefused("the rollup does not cover everything below the boundary")
    boundary_day, boundary_ts = boundary

    watermark = get_watermark(conn)
    if watermark is None:
        raise PruneRefused("the rollup has never completed a day")

    # Guard 4: the days actually about to lose their raw rows must be rolled.
    # Checked over the guild's own days — a guild whose history is entirely
    # inside the boundary has nothing to prove and nothing to delete.
    # DISTINCT first, then one probe per day: the correlated form ran the
    # subquery once per *row*, which on the busy guild is half a million
    # probes to answer a question about ~440 days.
    unrolled = conn.execute(
        """
        SELECT d FROM (
            SELECT DISTINCT date(created_at, 'unixepoch') AS d
            FROM xp_events
            WHERE guild_id = ? AND created_at < ?
        )
        WHERE NOT EXISTS (
            SELECT 1 FROM xp_daily r WHERE r.guild_id = ? AND r.day = d
        )
        LIMIT 1
        """,
        (guild_id, boundary_ts, guild_id),
    ).fetchone()
    if unrolled is not None:
        raise PruneRefused(f"day {unrolled[0]} has raw events but no rollup")

    # Guard 3, last because it is the cheapest to get wrong: the watermark is
    # the end of the contiguous rolled prefix, so requiring it to reach the day
    # before the boundary is what stops a hole further back being stepped over.
    oldest = conn.execute(
        "SELECT MIN(date(created_at, 'unixepoch')) FROM xp_events"
        " WHERE guild_id = ? AND created_at < ?",
        (guild_id, boundary_ts),
    ).fetchone()
    if oldest is None or oldest[0] is None:
        return 0
    if watermark < str(oldest[0]):
        raise PruneRefused(
            f"watermark {watermark} is behind the oldest prunable day {oldest[0]}"
        )

    cur = conn.execute(
        """
        DELETE FROM xp_events
        WHERE rowid IN (
            SELECT rowid FROM xp_events
            WHERE guild_id = ? AND created_at < ?
            ORDER BY created_at
            LIMIT ?
        )
        """,
        (guild_id, boundary_ts, limit),
    )
    deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if deleted:
        log.info(
            "xp retention: pruned %d raw event(s) below %s for guild %d",
            deleted, boundary_day, guild_id,
        )
    return deleted
