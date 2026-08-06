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


def rollup_day(conn: sqlite3.Connection, day: str) -> int:
    """Rebuild every ``xp_daily`` bucket for one UTC day, across all guilds.

    Returns the number of buckets written. Deletes the day first so a rebuild
    after events were themselves deleted (an erasure run, say) shrinks the
    rollup instead of leaving a stale total behind.
    """
    start, end = _day_bounds(day)

    conn.execute("DELETE FROM xp_daily WHERE day = ?", (day,))
    cur = conn.execute(
        """
        INSERT INTO xp_daily
            (guild_id, user_id, source, channel_id, day,
             xp, events, first_at, last_at)
        SELECT guild_id, user_id, source, channel_id, ?,
               SUM(amount), COUNT(*), MIN(created_at), MAX(created_at)
        FROM xp_events
        WHERE created_at >= ? AND created_at < ?
        GROUP BY guild_id, user_id, source, channel_id
        """,
        (day, start, end),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


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

    buckets = 0
    for day in pending:
        buckets += rollup_day(conn, day)
    if pending:
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
    buckets = 0
    for day in targets:
        buckets += rollup_day(conn, day)
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


def rollup_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Coverage summary, for the log line and for eyeballing a backfill."""
    raw_days = set(days_with_events(conn))
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
