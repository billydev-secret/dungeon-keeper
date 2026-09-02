"""Activity graph generation — message counts bucketed by time resolution."""

from __future__ import annotations

import bisect
import io
import math
import os
import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from bot_modules.core.bot_exclusion import bot_filter_clause, bot_ids_subquery
from bot_modules.services import xp_rollup_service

# The unit runs with ProtectHome=read-only, so matplotlib cannot write its
# default config dir (~/.config/matplotlib): it warns and falls back to a fresh
# temp dir on every boot, rebuilding the font cache each time. Point it at this
# repo-local dir (the unit's only ReadWritePath) BEFORE importing matplotlib,
# which resolves the path at import time. setdefault so an explicit
# MPLCONFIGDIR still wins.
os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parents[3] / ".cache" / "matplotlib"),
)

import matplotlib  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as ticker  # noqa: E402

matplotlib.use("Agg")

from bot_modules.services.pyplot_lock import (  # noqa: E402
    serialized_render as _serialized_render,
)

Resolution = Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"]

_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_HOD_LABELS = [
    "12am",
    "1am",
    "2am",
    "3am",
    "4am",
    "5am",
    "6am",
    "7am",
    "8am",
    "9am",
    "10am",
    "11am",
    "12pm",
    "1pm",
    "2pm",
    "3pm",
    "4pm",
    "5pm",
    "6pm",
    "7pm",
    "8pm",
    "9pm",
    "10pm",
    "11pm",
]

# Discord dark theme palette
_BG = "#2f3136"
_BAR = "#5865f2"
_BAR_ACCENT = "#eb459e"  # pink for unique-members line
_TEXT = "#dcddde"
_GRID = "#40444b"

# XP source palette — kept in lock-step with web/static/js/panels/activity.js
# so the slash command, the dashboard and the mod stats panel render the same
# product. These are the shared categorical slots from static/js/charts.js
# (ROLE_COLORS), not Discord's brand hues: the brand set this file used to carry
# had drifted out of that lock-step and fails the palette validator's lightness
# band (#57f287 at L 0.86, #fee75c at L 0.92 against a band of 0.48–0.67).
#
# Six slots, and no seventh. charts.js states the rule: past six, adjacent
# classes blur whatever you pick, so the tail folds into "Other" rather than
# taking a generated hue. ``grant`` is the tail — 41 events in the guild's whole
# history — and the two sources that came online in July get real identities
# instead of all three sharing one anonymous grey.
_SERIES_OVERFLOW = "#6b7076"
_XP_SOURCE_COLORS = {
    "text":           "#B58030",  # amber
    "reply":          "#4A7023",  # moss
    "image_react":    "#00A29C",  # teal
    "voice":          "#2167A1",  # slate
    "quest":          "#9D79C3",  # orchid
    "reaction_given": "#97435C",  # wine
}
_XP_SOURCE_LABELS = {
    "text":           "Messages",
    "reply":          "Reply bonus",
    "image_react":    "Image reaction",
    "voice":          "Voice",
    "quest":          "Quests",
    "reaction_given": "Reactions given",
    "grant":          "Manual grant",
    "other":          "Other",
}
_XP_SOURCE_FALLBACK = _SERIES_OVERFLOW
_XP_SOURCE_ORDER = [
    "text",
    "reply",
    "quest",
    "image_react",
    "voice",
    "reaction_given",
]

#: Public alias: callers that fold their own tail into "Other" need to know
#: which sources have a palette slot. The fold key is ``"other"``.
XP_SOURCE_ORDER = _XP_SOURCE_ORDER
XP_SOURCE_OTHER = "other"


# ---------------------------------------------------------------------------
# Bucket sequence builders
# ---------------------------------------------------------------------------


def _hour_buckets(
    now: datetime, utc_offset_hours: float = 0
) -> tuple[list[tuple[str, str]], float]:
    """24 hourly buckets ending at the current hour."""
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    start = local_now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    buckets = []
    for i in range(24):
        dt = start + timedelta(hours=i)
        key = (dt - offset).strftime("%Y-%m-%d %H")  # key in UTC for SQL match
        label = dt.strftime("%a %H:%M")  # label in local time
        buckets.append((key, label))
    return buckets, (start - offset).timestamp()


def _day_buckets(
    now: datetime, utc_offset_hours: float = 0
) -> tuple[list[tuple[str, str]], float]:
    """30 rolling 24-hour buckets ending at *now*.

    Each bucket spans exactly 24 hours.  The last bucket ends at *now*,
    so the rightmost bar always contains a full day of data regardless
    of the caller's timezone.
    """
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    start = local_now - timedelta(days=30)
    start_ts = (start - offset).timestamp()  # back to UTC for SQL
    buckets = []
    for i in range(30):
        bucket_end = start + timedelta(days=i + 1)
        key = str(int(start_ts + (i + 1) * 86400))
        label = bucket_end.strftime("%b %d")
        buckets.append((key, label))
    return buckets, start_ts


_WEEK_SECS = 7 * 86400
_MONTH_SECS = 30 * 86400


def _week_buckets(
    now: datetime, utc_offset_hours: float = 0
) -> tuple[list[tuple[str, str]], float]:
    """12 rolling 7-day buckets ending at now."""
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    start = local_now - timedelta(weeks=12)
    start_ts = (start - offset).timestamp()
    buckets = []
    for i in range(12):
        bucket_end = start + timedelta(weeks=i + 1)
        key = str(int(start_ts + (i + 1) * _WEEK_SECS))
        label = bucket_end.strftime("%b %d")
        buckets.append((key, label))
    return buckets, start_ts


def _month_buckets(
    now: datetime, utc_offset_hours: float = 0
) -> tuple[list[tuple[str, str]], float]:
    """12 rolling 30-day buckets ending at now."""
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    start = local_now - timedelta(days=30 * 12)
    start_ts = (start - offset).timestamp()
    buckets = []
    for i in range(12):
        bucket_end = start + timedelta(days=30 * (i + 1))
        key = str(int(start_ts + (i + 1) * _MONTH_SECS))
        label = bucket_end.strftime("%b %d")
        buckets.append((key, label))
    return buckets, start_ts


def _strftime_expr(
    resolution: Resolution,
    col: str = "created_at",
    since_ts: float = 0,
    utc_offset_secs: int = 0,
) -> str:
    """SQLite expression that buckets a timestamp column into the right key format.

    Day, week, and month resolutions use rolling windows anchored to the query
    start — the key is the epoch of the bucket's upper edge, so the last bucket
    always ends at *now* and is never partially filled.  Hour resolution uses
    calendar-hour strftime bucketing.

    *utc_offset_secs* shifts the timestamp before bucketing so that calendar
    boundaries align with the user's local time (hour resolution only).
    """
    shifted = f"({col} + {utc_offset_secs})" if utc_offset_secs else col
    if resolution == "hour":
        return f"strftime('%Y-%m-%d %H', datetime({shifted}, 'unixepoch'))"
    if resolution == "day":
        return (
            f"CAST(CAST(({col} - {since_ts}) / 86400 AS INTEGER) * 86400"
            f" + 86400 + {since_ts} AS INTEGER)"
        )
    if resolution == "week":
        return (
            f"CAST(CAST(({col} - {since_ts}) / {_WEEK_SECS} AS INTEGER) * {_WEEK_SECS}"
            f" + {_WEEK_SECS} + {since_ts} AS INTEGER)"
        )
    # month = 30-day rolling window
    return (
        f"CAST(CAST(({col} - {since_ts}) / {_MONTH_SECS} AS INTEGER) * {_MONTH_SECS}"
        f" + {_MONTH_SECS} + {since_ts} AS INTEGER)"
    )


_BUCKET_BUILDERS = {
    "hour": _hour_buckets,
    "day": _day_buckets,
    "week": _week_buckets,
    "month": _month_buckets,
}

# The XP hour-of-day / day-of-week histograms were unbounded all-time reads
# of xp_events. A *daily* rollup cannot answer "what hour was this" at all, and
# answers "what weekday" only approximately once a guild's UTC offset moves the
# day boundary — so unlike the bucketed graphs these two cannot union the
# rollup. They are therefore windowed to the same horizon raw events are kept
# for: past that point the honest answer is "we no longer store the hour".
# Stage 2b of docs/plans/xp-events-retention-and-rollup.md records the call.
XP_HISTOGRAM_WINDOW_DAYS = xp_rollup_service.RAW_RETENTION_DAYS


def xp_histogram_window_label(base: str) -> str:
    """`base` plus the window the XP histograms are now limited to."""
    return f"{base} (last {XP_HISTOGRAM_WINDOW_DAYS} days)"


_WINDOW_LABELS = {
    "hour": "Last 24 Hours",
    "day": "Last 30 Days",
    "week": "Last 12 Weeks",
    "month": "Last 12 Months",
    "hour_of_day": "By Hour of Day",
    "day_of_week": "By Day of Week",
}


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def _append_exclusions(
    where: str,
    params: list[object],
    exclude_user_ids: set[int] | None,
    exclude_channel_ids: set[int] | None,
) -> str:
    if exclude_user_ids:
        ph = ",".join("?" * len(exclude_user_ids))
        where += f" AND user_id NOT IN ({ph})"
        params.extend(exclude_user_ids)
    if exclude_channel_ids:
        ph = ",".join("?" * len(exclude_channel_ids))
        where += f" AND (channel_id IS NULL OR channel_id NOT IN ({ph}))"
        params.extend(exclude_channel_ids)
    return where



def _xp_row_source(
    conn: sqlite3.Connection, since_ts: float
) -> tuple[str, list[object]]:
    """FROM-clause source for XP graphs, unioning the rollup below the boundary.

    Stage 2b of docs/plans/xp-events-retention-and-rollup.md. Returns a SQL
    fragment with ``xp_events``' column names (``guild_id``, ``user_id``,
    ``source``, ``channel_id``, ``created_at``, ``amount``) and the leading
    parameters it needs, so a caller substitutes it for ``xp_events`` and
    prepends the params — every filter, exclusion and bucket expression then
    works unchanged.

    Below the retention boundary raw events may be gone, so ``xp_daily``
    answers for those days: one synthetic row per (user, source, channel, day)
    stamped at that UTC day's midnight. That is the accepted fidelity cost
    (option (a) in the plan) — a rolled-up day whose midnight falls on the far
    side of a rolling bucket edge is attributed whole to one bucket, so a
    360-day ``month`` graph can misplace up to a day of XP at each edge, and a
    member active only on such a day counts toward one bucket's
    ``COUNT(DISTINCT user_id)`` rather than both. Everything from the boundary
    forward is raw and exact, which is every bucket of the hour/day/week views.

    Returns plain ``xp_events`` when the rollup is not readable (nothing pruned
    yet, or the backfill is incomplete) or when the whole window sits inside
    the boundary — in both cases raw alone is complete *and* exact.
    """
    boundary = xp_rollup_service.read_boundary(conn)
    if boundary is None:
        return "xp_events", []
    boundary_day, boundary_ts = boundary
    if since_ts >= boundary_ts:
        return "xp_events", []
    return (
        """(
            SELECT guild_id, user_id, source, channel_id, created_at, amount
            FROM xp_events
            WHERE created_at >= ?
            UNION ALL
            SELECT guild_id, user_id, source, channel_id,
                   CAST(strftime('%s', day) AS REAL) AS created_at,
                   xp AS amount
            FROM xp_daily
            WHERE day < ? AND day >= ?
        )""",
        [boundary_ts, boundary_day, xp_rollup_service.utc_day(since_ts)],
    )


def query_message_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> tuple[list[str], list[int], list[int]]:
    """
    Query message counts and unique active members per time bucket.

    Returns (labels, message_counts, unique_member_counts).
    Empty buckets are filled with 0.
    """
    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now, utc_offset_hours)
    offset_secs = int(utc_offset_hours * 3600)
    bucket_expr = _strftime_expr(
        resolution, since_ts=since_ts, utc_offset_secs=offset_secs
    )

    params: list[object] = [guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    rows = conn.execute(
        f"""
        SELECT
            {bucket_expr} AS bucket,
            COUNT(*) AS msg_count,
            COUNT(DISTINCT user_id) AS member_count
        FROM processed_messages
        WHERE {where}
        GROUP BY bucket
        """,
        params,
    ).fetchall()

    msg_by_key = {str(row[0]): int(row[1]) for row in rows}
    members_by_key = {str(row[0]): int(row[2]) for row in rows}

    labels = [label for _, label in bucket_sequence]
    msg_counts = [msg_by_key.get(key, 0) for key, _ in bucket_sequence]
    member_counts = [members_by_key.get(key, 0) for key, _ in bucket_sequence]

    return labels, msg_counts, member_counts


def query_message_histogram(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Literal["hour_of_day", "day_of_week"],
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> tuple[list[str], list[int]]:
    """
    Aggregate message counts by hour-of-day (0-23) or day-of-week (0=Sun..6=Sat)
    across all recorded history.

    Returns (labels, message_counts).
    """
    offset_secs = int(utc_offset_hours * 3600)
    shifted = f"(created_at + {offset_secs})" if offset_secs else "created_at"
    if resolution == "hour_of_day":
        expr = f"CAST(strftime('%H', datetime({shifted}, 'unixepoch')) AS INTEGER)"
        labels = _HOD_LABELS
        n = 24
    else:
        expr = f"CAST(strftime('%w', datetime({shifted}, 'unixepoch')) AS INTEGER)"
        labels = _DOW_LABELS
        n = 7

    params: list[object] = [guild_id]
    where = "guild_id = ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    rows = conn.execute(
        f"""
        SELECT {expr} AS bucket, COUNT(*) AS msg_count
        FROM processed_messages
        WHERE {where}
        GROUP BY bucket
        """,
        params,
    ).fetchall()

    counts_by_bucket = {int(row[0]): int(row[1]) for row in rows}
    return labels, [counts_by_bucket.get(i, 0) for i in range(n)]


# ---------------------------------------------------------------------------
# XP activity queries
# ---------------------------------------------------------------------------


def query_xp_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> tuple[list[str], list[float], list[int]]:
    """
    Query XP earned and unique active members per time bucket.

    Returns (labels, xp_totals, unique_member_counts).
    """
    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now, utc_offset_hours)
    offset_secs = int(utc_offset_hours * 3600)
    bucket_expr = _strftime_expr(
        resolution, since_ts=since_ts, utc_offset_secs=offset_secs
    )

    src, params = _xp_row_source(conn, since_ts)
    params = [*params, guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    rows = conn.execute(
        f"""
        SELECT
            {bucket_expr} AS bucket,
            COALESCE(SUM(amount), 0) AS xp_total,
            COUNT(DISTINCT user_id) AS member_count
        FROM {src}
        WHERE {where}
        GROUP BY bucket
        """,
        params,
    ).fetchall()

    xp_by_key = {str(row[0]): float(row[1]) for row in rows}
    members_by_key = {str(row[0]): int(row[2]) for row in rows}

    labels = [label for _, label in bucket_sequence]
    xp_totals = [round(xp_by_key.get(key, 0.0), 1) for key, _ in bucket_sequence]
    member_counts = [members_by_key.get(key, 0) for key, _ in bucket_sequence]

    return labels, xp_totals, member_counts


def query_xp_histogram(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Literal["hour_of_day", "day_of_week"],
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> tuple[list[str], list[float]]:
    """
    Aggregate XP earned by hour-of-day or day-of-week.

    Returns (labels, xp_totals).
    """
    offset_secs = int(utc_offset_hours * 3600)
    shifted = f"(created_at + {offset_secs})" if offset_secs else "created_at"
    if resolution == "hour_of_day":
        expr = f"CAST(strftime('%H', datetime({shifted}, 'unixepoch')) AS INTEGER)"
        labels = _HOD_LABELS
        n = 24
    else:
        expr = f"CAST(strftime('%w', datetime({shifted}, 'unixepoch')) AS INTEGER)"
        labels = _DOW_LABELS
        n = 7

    # Windowed, not all-time: see XP_HISTOGRAM_WINDOW_DAYS.
    since_ts = datetime.now(timezone.utc).timestamp() - XP_HISTOGRAM_WINDOW_DAYS * 86400
    params: list[object] = [guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    rows = conn.execute(
        f"""
        SELECT {expr} AS bucket, COALESCE(SUM(amount), 0) AS xp_total
        FROM xp_events
        WHERE {where}
        GROUP BY bucket
        """,
        params,
    ).fetchall()

    totals_by_bucket = {int(row[0]): round(float(row[1]), 1) for row in rows}
    return labels, [totals_by_bucket.get(i, 0.0) for i in range(n)]


def query_xp_activity_with_breakdown(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> tuple[list[str], list[float], list[int], dict[str, list[float]]]:
    """Same as ``query_xp_activity`` but also returns per-source XP totals.

    Returns (labels, xp_totals, unique_member_counts, by_source) where
    ``by_source`` maps each ``xp_events.source`` value to a list of XP totals
    aligned to ``labels``.
    """
    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now, utc_offset_hours)
    offset_secs = int(utc_offset_hours * 3600)
    bucket_expr = _strftime_expr(
        resolution, since_ts=since_ts, utc_offset_secs=offset_secs
    )

    src, params = _xp_row_source(conn, since_ts)
    params = [*params, guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    members_rows = conn.execute(
        f"""
        SELECT {bucket_expr} AS bucket,
               COUNT(DISTINCT user_id) AS member_count
        FROM {src}
        WHERE {where}
        GROUP BY bucket
        """,
        params,
    ).fetchall()

    source_rows = conn.execute(
        f"""
        SELECT {bucket_expr} AS bucket, source,
               COALESCE(SUM(amount), 0) AS xp_total
        FROM {src}
        WHERE {where}
        GROUP BY bucket, source
        """,
        params,
    ).fetchall()

    keys = [k for k, _ in bucket_sequence]
    key_to_idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)

    members_by_key = {str(row[0]): int(row[1]) for row in members_rows}

    by_source: dict[str, list[float]] = {}
    for bucket_key, src, total in source_rows:
        idx = key_to_idx.get(str(bucket_key))
        if idx is None:
            continue
        series = by_source.setdefault(str(src), [0.0] * n)
        series[idx] = round(float(total), 1)

    labels = [label for _, label in bucket_sequence]
    xp_totals = [
        round(sum(series[i] for series in by_source.values()), 1)
        for i in range(n)
    ]
    member_counts = [members_by_key.get(k, 0) for k in keys]
    return labels, xp_totals, member_counts, by_source


def query_xp_all_time_with_breakdown(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
) -> tuple[list[str], list[float], dict[str, list[float]], dict[str, int]]:
    """Weekly XP per source across the guild's whole history.

    Unlike every other graph in this module the window does not roll: it starts
    at the guild's first XP event and runs to now, so the bar count grows with
    the server rather than staying at a fixed 12 or 30.

    Weeks, not days: a guild a year old is 365 daily bars in a picture Discord
    renders about 400px wide on a phone, which is not a chart. Weeks, not
    months, because the shape this exists to show — a new XP source coming
    online and the stack getting taller for a reason that is not the community
    getting busier — disappears into a monthly average.

    Returns ``(labels, totals, by_source, source_starts)``. ``source_starts``
    maps each source to the index of the first bucket it earned anything in,
    which is what the panel draws its "this source started here" rules from:
    without them the stack growing two new colours in one week reads as a surge
    in activity rather than as a change in what the bot pays XP for.

    Reads through :func:`_xp_row_source`, so it stays correct once raw
    ``xp_events`` below the retention boundary have been pruned to ``xp_daily``.
    """
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    # since_ts=0 so the union covers the rollup as well as raw: the guild's
    # first event may well sit below the retention boundary.
    src, src_params = _xp_row_source(conn, 0)
    row = conn.execute(
        f"SELECT MIN(created_at) FROM {src} WHERE guild_id = ?",
        [*src_params, guild_id],
    ).fetchone()
    if row is None or row[0] is None:
        return [], [], {}, {}
    start_ts = float(row[0])

    bucket_count = max(1, math.ceil((now_ts - start_ts) / _WEEK_SECS))
    keys = [str(int(start_ts + (i + 1) * _WEEK_SECS)) for i in range(bucket_count)]
    labels = [
        datetime.fromtimestamp(
            start_ts + (i + 1) * _WEEK_SECS, timezone.utc
        ).strftime("%b %d")
        for i in range(bucket_count)
    ]
    key_to_idx = {k: i for i, k in enumerate(keys)}

    bucket_expr = _strftime_expr("week", since_ts=start_ts)
    params: list[object] = [*src_params, guild_id, start_ts]
    where = "guild_id = ? AND created_at >= ?"
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    rows = conn.execute(
        f"""
        SELECT {bucket_expr} AS bucket, source,
               COALESCE(SUM(amount), 0) AS xp_total
        FROM {src}
        WHERE {where}
        GROUP BY bucket, source
        """,
        params,
    ).fetchall()

    by_source: dict[str, list[float]] = {}
    for bucket_key, source, total in rows:
        idx = key_to_idx.get(str(bucket_key))
        if idx is None:
            continue
        series = by_source.setdefault(str(source), [0.0] * bucket_count)
        series[idx] = round(float(total), 1)

    source_starts = {
        source: next(i for i, v in enumerate(values) if v)
        for source, values in by_source.items()
        if any(values)
    }
    totals = [
        round(sum(series[i] for series in by_source.values()), 1)
        for i in range(bucket_count)
    ]
    return labels, totals, by_source, source_starts


def query_xp_histogram_with_breakdown(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Literal["hour_of_day", "day_of_week"],
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> tuple[list[str], list[float], dict[str, list[float]]]:
    """Same as ``query_xp_histogram`` but also returns per-source XP totals."""
    offset_secs = int(utc_offset_hours * 3600)
    shifted = f"(created_at + {offset_secs})" if offset_secs else "created_at"
    if resolution == "hour_of_day":
        expr = f"CAST(strftime('%H', datetime({shifted}, 'unixepoch')) AS INTEGER)"
        labels = _HOD_LABELS
        n = 24
    else:
        expr = f"CAST(strftime('%w', datetime({shifted}, 'unixepoch')) AS INTEGER)"
        labels = _DOW_LABELS
        n = 7

    # Windowed, not all-time: see XP_HISTOGRAM_WINDOW_DAYS.
    since_ts = datetime.now(timezone.utc).timestamp() - XP_HISTOGRAM_WINDOW_DAYS * 86400
    params: list[object] = [guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    rows = conn.execute(
        f"""
        SELECT {expr} AS bucket, source,
               COALESCE(SUM(amount), 0) AS xp_total
        FROM xp_events
        WHERE {where}
        GROUP BY bucket, source
        """,
        params,
    ).fetchall()

    by_source: dict[str, list[float]] = {}
    for bucket, src, total in rows:
        idx = int(bucket)
        if not (0 <= idx < n):
            continue
        series = by_source.setdefault(str(src), [0.0] * n)
        series[idx] = round(float(total), 1)

    totals = [
        round(sum(series[i] for series in by_source.values()), 1)
        for i in range(n)
    ]
    return labels, totals, by_source


# ---------------------------------------------------------------------------
# Period overlay (this day/week against a band of the last N)
# ---------------------------------------------------------------------------
#
# See docs/plans/weekly-activity-comparison.md. Unlike every resolution above,
# the x-axis here is a position *inside* a period rather than a timeline: the
# current period is drawn against a p25-p75 envelope over the last N complete
# ones, so "is this week up or down, and when" is one glance.

OverlayPeriod = Literal["day", "week"]

_OVERLAY_PERIOD_SECS: dict[str, int] = {"day": 86400, "week": _WEEK_SECS}

# Below this many *sampled* periods a percentile band is not a summary of
# anything - the p25 and p75 of two weeks are just the two weeks. The current
# period still draws; the band is suppressed and the caller says so.
MIN_BAND_PERIODS = 3

# The furthest back each overlay will look, before the mode's own reach is
# applied. Not a data limit — a legibility one: a band over more periods than
# this stops changing shape, and the request gets slower for nothing.
OVERLAY_MAX_PERIODS: dict[str, int] = {"day": 90, "week": 26}

# A same-weekday day overlay steps a week at a time, so it reaches back as far
# as the weekly overlay does and is capped like that rather than like a day.
OVERLAY_SAME_WEEKDAY_MAX = 26

# Width, in hours, of the centred rolling mean the period *in progress* is
# drawn with. Only that line is smoothed: the band is already an average of its
# own (a per-hour percentile over N periods), while the current period is a
# single realisation and reads as hash at hour resolution. A week is 168 points
# on an axis that can only label a tick a day, so three hours takes the noise
# out without moving where the peaks sit; a day is 24 points whose hour-by-hour
# shape *is* the reading, so it is left alone.
#
# The raw counts travel alongside it and are what the table and the period
# total are built from - the smoothing is a line on a chart, never the number
# anyone reads off.
OVERLAY_SMOOTH_WINDOW: dict[str, int] = {"day": 1, "week": 3}


def smooth_series(values: Sequence[float | None], window: int) -> list[float | None]:
    """Centred rolling mean of *values*, keeping ``None`` where it found one.

    The window **truncates** at both ends rather than wrapping or zero-padding:
    hour 0 of a period has no hour before it, and the hour being lived through
    has no hour after it. Averaging either against a fabricated neighbour would
    bend the line toward the floor exactly where the reader is looking hardest -
    the start of the period, and the live edge.
    """
    if window <= 1:
        return list(values)
    half = window // 2
    out: list[float | None] = []
    for i, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        near = [v for v in values[max(0, i - half) : i + half + 1] if v is not None]
        out.append(round(sum(near) / len(near), 1))
    return out

# Full weekday names, Sunday-first to match _DOW_LABELS. Spelled out rather
# than taken from strftime("%A"), which follows the process locale.
_DOW_FULL = [
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
]


def overlay_stride_days(period: OverlayPeriod, same_weekday: bool = False) -> int:
    """Days between one sampled period and the one before it.

    Ordinarily a period's own length. A same-weekday day overlay samples every
    seventh day instead, which is the whole point of it: a Tuesday read against
    a mixed bag of weekend days says more about the weekend than about Tuesday.
    """
    if same_weekday and period == "day":
        return 7
    return _OVERLAY_PERIOD_SECS[period] // 86400


def overlay_period_cap(
    period: OverlayPeriod, mode: str, same_weekday: bool = False
) -> int:
    """Largest N this period and mode can answer honestly.

    XP is bounded by raw retention because the overlay cannot union the daily
    rollup (see :func:`query_activity_overlay`). Deliberately derived from the
    retention *policy* rather than from what ``xp_events`` happens to hold
    today: prod has not run the pruner yet, so a cap measured off the live
    table would silently shrink the first time it does.

    Reach is measured in days *spanned*, not periods, so "the last 12 Tuesdays"
    (84 days) is answerable on XP where "the last 13" (91 days) is not.
    """
    if same_weekday and period == "day":
        hard = OVERLAY_SAME_WEEKDAY_MAX
    else:
        hard = OVERLAY_MAX_PERIODS[period]
    if mode != "xp":
        return hard
    stride_days = overlay_stride_days(period, same_weekday)
    return max(1, min(hard, XP_HISTOGRAM_WINDOW_DAYS // stride_days))


def overlay_weekday_name(now: datetime, utc_offset_hours: float) -> str:
    """The guild-local weekday *now* falls on, e.g. ``"Tuesday"``.

    Read off the guild's own clock, not UTC: at 18:00 on a Saturday in a UTC-7
    guild it is already Sunday in UTC, and a band labelled "Sundays" drawn over
    Saturday data is the exact misread this view exists to prevent.
    """
    local = now + timedelta(hours=utc_offset_hours)
    return _DOW_FULL[(local.weekday() + 1) % 7]


def overlay_period_start(
    now: datetime, utc_offset_hours: float, period: OverlayPeriod
) -> float:
    """UTC epoch of the start of the guild-local day/week containing *now*.

    The anchor is a *local* midnight (and for ``week``, a local Sunday
    midnight), which is what makes the rest of the overlay code free of
    timezone arithmetic: once the window starts on a local period boundary,
    ``created_at - since_ts`` measures elapsed time from that boundary, so both
    the period index and the hour within the period fall straight out of it. An
    off-by-one-timezone bucket is the classic bug in this shape of query, and
    this is where it is prevented.
    """
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        # _DOW_LABELS is Sunday-first, so the week is too.
        local_start -= timedelta(days=(local_now.weekday() + 1) % 7)
    return (local_start - offset).timestamp()


def overlay_labels(period: OverlayPeriod) -> list[str]:
    """One label per hour of the period - 24 for a day, 168 for a week."""
    if period == "day":
        return list(_HOD_LABELS)
    return [f"{dow} {hod}" for dow in _DOW_LABELS for hod in _HOD_LABELS]


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile of *values* (unsorted), q in 0..1."""
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return float(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo))


@dataclass
class OverlayResult:
    """The current period's curve plus the band it is read against."""

    labels: list[str]
    #: The period in progress. ``None`` past the current hour - see below.
    current: list[float | None]
    band_low: list[float]   # p25 over the sampled periods
    band_mid: list[float]   # p50
    band_high: list[float]  # p75
    periods_requested: int
    periods_sampled: int
    #: True when the window was shortened to stay inside XP raw retention.
    clamped: bool
    #: True when the band sampled only days sharing today's weekday.
    same_weekday: bool = False
    #: ``current`` under a centred rolling mean, for drawing only. Empty when
    #: this period is not smoothed at all (see OVERLAY_SMOOTH_WINDOW).
    current_smooth: list[float | None] = field(default_factory=list)
    #: Width of that mean in hours; 1 when the line is drawn raw.
    smooth_window: int = 1

    @property
    def has_band(self) -> bool:
        return len(self.band_mid) > 0


def query_activity_overlay(
    conn: sqlite3.Connection,
    guild_id: int,
    period: OverlayPeriod,
    *,
    mode: Literal["messages", "xp"] = "messages",
    compare_periods: int = 12,
    same_weekday: bool = False,
    user_id: int | None = None,
    include_user_ids: set[int] | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    utc_offset_hours: float = 0,
) -> OverlayResult:
    """Current day/week against a percentile band over the last N.

    ``mode="xp"`` reads **raw** ``xp_events`` only and never unions the daily
    rollup the way :func:`_xp_row_source` does for the timeline resolutions.
    ``xp_daily`` stamps each synthetic row at its UTC day's midnight, which
    carries no hour-of-day information at all: every pre-boundary row would
    land in a single hour bucket, inventing a spike at midnight and a trough
    across the other 23 hours. Worse, a guild at a negative offset (the main
    guild runs UTC-7) has that UTC midnight fall at 17:00 the *previous* local
    day, so the invented spike lands on the wrong weekday too.

    So the window is clamped to the retention boundary instead, exactly as the
    hour-of-day/day-of-week XP histograms already are, and ``clamped`` says it
    happened so the caption can be honest rather than quietly short.

    ``same_weekday`` (day overlay only) samples every *seventh* day back from
    today rather than every day, so a Tuesday is read against Tuesdays. Weekday
    seasonality dominates a server's rhythm, and a band mixing weekends into a
    weekday's history mostly measures the weekend.

    ``include_user_ids`` narrows to a *group* of members, where ``user_id``
    narrows to one. A falsy value (None or an empty set) applies no filter at
    all, so a caller holding an empty group must not call rather than expect
    zeros: an empty ``IN ()`` is a SQL error, and silently widening to the
    whole guild would draw the server's own line and label it the group's.
    """
    period_secs = _OVERLAY_PERIOD_SECS[period]
    n_buckets = period_secs // 3600
    now = datetime.now(timezone.utc)
    current_start = overlay_period_start(now, utc_offset_hours, period)

    # The gap between one sampled period and the previous one. It is only ever
    # wider than the period itself for the same-weekday day view, where each
    # step skips the six days in between - so the period index divides by the
    # stride while the hour within a period still divides by the period.
    same_weekday = bool(same_weekday) and period == "day"
    step_secs = overlay_stride_days(period, same_weekday) * 86400

    periods = max(1, int(compare_periods))
    since_ts = current_start - periods * step_secs

    clamped = False
    if mode == "xp":
        boundary = xp_rollup_service.read_boundary(conn)
        if boundary is not None:
            _, boundary_ts = boundary
            if since_ts < boundary_ts:
                periods = max(0, int((current_start - boundary_ts) // step_secs))
                since_ts = current_start - periods * step_secs
                clamped = True

    since_i = int(since_ts)
    elapsed = f"(CAST(created_at AS INTEGER) - {since_i})"
    idx_expr = f"CAST({elapsed} / {step_secs} AS INTEGER)"
    hour_expr = f"CAST(({elapsed} % {step_secs}) / 3600 AS INTEGER)"

    params: list[object] = [guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if step_secs != period_secs:
        # `since_ts` is a local midnight a whole number of weeks back, so each
        # stride block opens on today's weekday: keeping only the block's first
        # `period_secs` keeps that day and drops the six behind it. Filtered in
        # SQL rather than in the loop below so six days out of seven are never
        # read at all.
        where += f" AND {elapsed} % {step_secs} < {period_secs}"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if include_user_ids:
        # The set-valued counterpart of ``user_id``: restrict to a *group*
        # rather than one member. Sorted so the SQL text and its params are
        # stable across calls with the same group, which keeps the statement
        # cache warm and makes a failing test's diff readable.
        ph = ",".join("?" * len(include_user_ids))
        where += f" AND user_id IN ({ph})"
        params.extend(sorted(include_user_ids))
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)
    where = _append_exclusions(where, params, exclude_user_ids, exclude_channel_ids)

    if mode == "xp":
        table, value_expr = "xp_events", "COALESCE(SUM(amount), 0)"
    else:
        table, value_expr = "processed_messages", "COUNT(*)"

    rows = conn.execute(
        f"""
        SELECT {idx_expr} AS pidx, {hour_expr} AS hidx, {value_expr} AS total
        FROM {table}
        WHERE {where}
        GROUP BY pidx, hidx
        """,
        params,
    ).fetchall()

    # Row `periods` is the period in progress; 0..periods-1 are the history.
    grid = [[0.0] * n_buckets for _ in range(periods + 1)]
    for pidx, hidx, total in rows:
        pi, hi = int(pidx), int(hidx)
        if 0 <= pi <= periods and 0 <= hi < n_buckets:
            grid[pi][hi] = float(total)

    # The current period is partial. Past the hour we are actually in, the
    # buckets are unlived rather than empty - zero-filling them would draw a
    # cliff to the floor and read as a collapse in activity, so they are None
    # and Chart.js leaves them unplotted.
    now_hour = int((now.timestamp() - current_start) // 3600)
    current: list[float | None] = [
        round(grid[periods][h], 1) if h <= now_hour else None
        for h in range(n_buckets)
    ]

    # A period with no rows at all predates the archive; counting it as a row
    # of zeros would drag the band toward the floor for a reason that is an
    # artefact of when logging started, not a fact about the server.
    sampled = [grid[i] for i in range(periods) if sum(grid[i]) > 0]

    band_low: list[float] = []
    band_mid: list[float] = []
    band_high: list[float] = []
    if len(sampled) >= MIN_BAND_PERIODS:
        for h in range(n_buckets):
            column = [wk[h] for wk in sampled]
            band_low.append(round(_percentile(column, 0.25), 1))
            band_mid.append(round(_percentile(column, 0.50), 1))
            band_high.append(round(_percentile(column, 0.75), 1))

    # A drawn-only companion to `current`. Computed here rather than in the
    # panel so one definition of the line's shape serves the chart, the API and
    # the tests, and so the raw series stays untouched beside it.
    smooth_window = OVERLAY_SMOOTH_WINDOW.get(period, 1)

    return OverlayResult(
        labels=overlay_labels(period),
        current=current,
        current_smooth=(
            smooth_series(current, smooth_window) if smooth_window > 1 else []
        ),
        smooth_window=smooth_window,
        band_low=band_low,
        band_mid=band_mid,
        band_high=band_high,
        periods_requested=max(1, int(compare_periods)),
        periods_sampled=len(sampled),
        clamped=clamped,
        same_weekday=same_weekday,
    )


# ---------------------------------------------------------------------------
# Message-rate drop analysis
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Enriched dropoff profiles
# ---------------------------------------------------------------------------


@dataclass
class DropoffProfile:
    """Rich engagement profile comparing two consecutive time windows."""

    user_id: int
    # Messages
    msgs_prev: int
    msgs_recent: int
    # Voice XP
    voice_xp_prev: float
    voice_xp_recent: float
    # Days active (out of days_in_window)
    days_prev: int
    days_recent: int
    days_in_window: int
    # Channels active
    channels_prev: int
    channels_recent: int
    # Replies sent
    replies_prev: int
    replies_recent: int
    # Conversation initiations (messages with no reply_to)
    initiations_prev: int
    initiations_recent: int
    # Average message length (chars)
    avg_len_prev: float
    avg_len_recent: float
    # Unique interaction partners (outbound)
    partners_prev: int
    partners_recent: int
    # Inbound interactions (others → this user)
    inbound_prev: int
    inbound_recent: int
    # Outbound interactions (this user → others)
    outbound_prev: int
    outbound_recent: int
    # Attachments sent
    attachments_prev: int
    attachments_recent: int
    # Reactions received (sum of reaction counts on their messages)
    reactions_prev: int
    reactions_recent: int
    # Peak posting hour (0-23, None if no messages)
    peak_hour_prev: int | None
    peak_hour_recent: int | None
    # Weekday message percentage (Mon-Fri)
    weekday_pct_prev: float
    weekday_pct_recent: float
    # Longest silence gap in recent window (seconds)
    longest_gap_secs: float
    # Last activity (unix timestamp)
    last_seen_ts: float | None
    # XP breakdown by source
    text_xp_prev: float
    text_xp_recent: float
    reply_xp_prev: float
    reply_xp_recent: float
    image_react_xp_prev: float
    image_react_xp_recent: float
    # Current level and total XP
    level: int
    total_xp: float
    # Channel migration (detail view) — channel IDs
    channels_left: list[int]
    channels_joined: list[int]
    channels_stayed: list[int]
    # Conversation depth (reply chains of 3+ the user participated in)
    deep_convos_prev: int
    deep_convos_recent: int
    # Days into recent window before first message
    first_activity_day: int | None
    # Server-wide baseline (same for all profiles in a batch)
    server_msgs_prev: int
    server_msgs_recent: int


def query_message_rate_drops(
    conn: sqlite3.Connection,
    guild_id: int,
    period_seconds: float,
    *,
    channel_id: int | None = None,
    min_previous: int = 5,
    limit: int = 10,
) -> list[tuple[int, int, int]]:
    """Compare per-user message counts across two consecutive equal-length windows.

    The full window spans ``2 * period_seconds`` ending now.  The midpoint divides
    it into a *previous* half and a *recent* half.

    Returns a list of ``(user_id, previous_count, recent_count)`` sorted by
    largest absolute drop, restricted to users whose previous count is at least
    ``min_previous`` and whose recent count is lower than their previous count.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    mid = now - int(period_seconds)
    start = mid - int(period_seconds)

    channel_clause = "AND channel_id = ? " if channel_id is not None else ""

    params: list[object] = [mid, mid, guild_id, start, now]
    if channel_id is not None:
        params.append(channel_id)
    params.extend([min_previous, limit])

    rows = conn.execute(
        f"""
        SELECT
            user_id,
            SUM(CASE WHEN created_at < ? THEN 1 ELSE 0 END) AS prev_count,
            SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent_count
        FROM processed_messages
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
        {channel_clause}
        GROUP BY user_id
        HAVING prev_count >= ? AND prev_count > recent_count
        ORDER BY (prev_count - recent_count) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


def query_dropoff_profiles(
    conn: sqlite3.Connection,
    guild_id: int,
    period_seconds: float,
    *,
    channel_id: int | None = None,
    min_previous: int = 5,
    limit: int = 10,
    target_user_id: int | None = None,
    include_bots: bool = False,
) -> list[DropoffProfile]:
    """Compute enriched engagement profiles for users with message-rate drops.

    If *target_user_id* is given, returns a single-element list with that user's
    profile regardless of whether they had a dropoff (useful for the detail view).
    Candidate selection honors *channel_id*; enrichment queries are server-wide.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    mid = now_ts - int(period_seconds)
    start = mid - int(period_seconds)
    days_in_window = max(1, round(period_seconds / 86400))

    # ── baseline (server-wide, or channel-scoped when filtering) ───────────
    if channel_id is not None:
        srv_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN created_at < ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ? AND created_at < ?
                  AND channel_id = ?
            """,
            [mid, mid, guild_id, start, now_ts, channel_id],
        ).fetchone()
    else:
        srv_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN created_at < ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ? AND created_at < ?
            """,
            [mid, mid, guild_id, start, now_ts],
        ).fetchone()
    srv_prev = int(srv_row[0] or 0) if srv_row else 0
    srv_recent = int(srv_row[1] or 0) if srv_row else 0

    # ── candidate selection ───────────────────────────────────────────────
    if target_user_id is not None:
        ch_clause = "AND channel_id = ? " if channel_id else ""
        params: list[object] = [mid, mid, guild_id, start, now_ts]
        if channel_id:
            params.append(channel_id)
        params.append(target_user_id)
        row = conn.execute(
            f"""
            SELECT user_id,
                   SUM(CASE WHEN created_at < ? THEN 1 ELSE 0 END),
                   SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ? AND created_at < ?
            {ch_clause}AND user_id = ?
            GROUP BY user_id
            """,
            params,
        ).fetchone()
        candidates = [
            (target_user_id, int(row[1]) if row else 0, int(row[2]) if row else 0)
        ]
    else:
        candidates = query_message_rate_drops(
            conn,
            guild_id,
            period_seconds,
            channel_id=channel_id,
            min_previous=min_previous,
            limit=limit,
        )

    # Every enrichment query below is scoped by ``author_id IN (user_ids)``, so
    # filtering the candidate list here is the single point that keeps bots out
    # of the whole profile set.
    if not include_bots and candidates:
        bot_ids = {
            r[0] for r in conn.execute(bot_ids_subquery(), (guild_id,)).fetchall()
        }
        candidates = [c for c in candidates if c[0] not in bot_ids]

    if not candidates:
        return []

    user_ids = [c[0] for c in candidates]
    msg_map: dict[int, tuple[int, int]] = {c[0]: (c[1], c[2]) for c in candidates}
    ph = ",".join("?" * len(user_ids))

    # ── messages table: channels, replies, initiations, avg len, weekday ──
    msg_rows = conn.execute(
        f"""
        SELECT author_id,
            COUNT(DISTINCT CASE WHEN ts < ? THEN channel_id END),
            COUNT(DISTINCT CASE WHEN ts >= ? THEN channel_id END),
            SUM(CASE WHEN ts < ? AND reply_to_id IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN ts >= ? AND reply_to_id IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN ts < ? AND reply_to_id IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN ts >= ? AND reply_to_id IS NULL THEN 1 ELSE 0 END),
            AVG(CASE WHEN ts < ? AND content IS NOT NULL THEN LENGTH(content) END),
            AVG(CASE WHEN ts >= ? AND content IS NOT NULL THEN LENGTH(content) END),
            SUM(CASE WHEN ts < ? AND CAST(strftime('%w', datetime(ts, 'unixepoch')) AS INTEGER)
                BETWEEN 1 AND 5 THEN 1.0 ELSE 0.0 END),
            SUM(CASE WHEN ts < ? THEN 1.0 ELSE 0.0 END),
            SUM(CASE WHEN ts >= ? AND CAST(strftime('%w', datetime(ts, 'unixepoch')) AS INTEGER)
                BETWEEN 1 AND 5 THEN 1.0 ELSE 0.0 END),
            SUM(CASE WHEN ts >= ? THEN 1.0 ELSE 0.0 END)
        FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ?
        AND author_id IN ({ph})
        GROUP BY author_id
        """,
        [mid] * 12 + [guild_id, start, now_ts] + user_ids,
    ).fetchall()

    msg_data: dict[int, dict] = {}
    for r in msg_rows:
        uid = int(r[0])
        total_prev = float(r[10]) or 1.0
        total_recent = float(r[12]) or 1.0
        msg_data[uid] = {
            "ch_p": int(r[1]),
            "ch_r": int(r[2]),
            "re_p": int(r[3]),
            "re_r": int(r[4]),
            "in_p": int(r[5]),
            "in_r": int(r[6]),
            "al_p": float(r[7] or 0),
            "al_r": float(r[8] or 0),
            "wd_p": float(r[9]) / total_prev * 100,
            "wd_r": float(r[11]) / total_recent * 100,
        }

    # ── xp_events: XP by source ──────────────────────────────────────────
    xp_rows = conn.execute(
        f"""
        SELECT user_id, source,
            SUM(CASE WHEN created_at < ? THEN amount ELSE 0 END),
            SUM(CASE WHEN created_at >= ? THEN amount ELSE 0 END)
        FROM xp_events
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
              AND user_id IN ({ph})
        GROUP BY user_id, source
        """,
        [mid, mid, guild_id, start, now_ts, *user_ids],
    ).fetchall()
    xp_map: dict[int, dict[str, tuple[float, float]]] = {}
    for r in xp_rows:
        uid = int(r[0])
        xp_map.setdefault(uid, {})[str(r[1])] = (float(r[2]), float(r[3]))

    # ── member_xp: current level ─────────────────────────────────────────
    level_rows = conn.execute(
        f"""
        SELECT user_id, level, total_xp FROM member_xp
        WHERE guild_id = ? AND user_id IN ({ph})
        """,
        [guild_id, *user_ids],
    ).fetchall()
    level_map: dict[int, tuple[int, float]] = {
        int(r[0]): (int(r[1]), float(r[2])) for r in level_rows
    }

    # ── processed_messages: days active ───────────────────────────────────
    days_rows = conn.execute(
        f"""
        SELECT user_id,
            COUNT(DISTINCT CASE WHEN created_at < ?
                  THEN DATE(datetime(created_at, 'unixepoch')) END),
            COUNT(DISTINCT CASE WHEN created_at >= ?
                  THEN DATE(datetime(created_at, 'unixepoch')) END)
        FROM processed_messages
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
              AND user_id IN ({ph})
        GROUP BY user_id
        """,
        [mid, mid, guild_id, start, now_ts, *user_ids],
    ).fetchall()
    days_map: dict[int, tuple[int, int]] = {
        int(r[0]): (int(r[1]), int(r[2])) for r in days_rows
    }

    # ── user_interactions_log: outbound partners & count ──────────────────
    out_rows = conn.execute(
        f"""
        SELECT from_user_id,
            COUNT(DISTINCT CASE WHEN ts < ? THEN to_user_id END),
            COUNT(DISTINCT CASE WHEN ts >= ? THEN to_user_id END),
            SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END)
        FROM user_interactions_log
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND from_user_id IN ({ph})
        GROUP BY from_user_id
        """,
        [mid] * 4 + [guild_id, start, now_ts] + user_ids,
    ).fetchall()
    out_map: dict[int, tuple[int, int, int, int]] = {
        int(r[0]): (int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in out_rows
    }

    # ── user_interactions_log: inbound count ──────────────────────────────
    in_rows = conn.execute(
        f"""
        SELECT to_user_id,
            SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END)
        FROM user_interactions_log
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND to_user_id IN ({ph})
        GROUP BY to_user_id
        """,
        [mid, mid, guild_id, start, now_ts] + user_ids,
    ).fetchall()
    in_map: dict[int, tuple[int, int]] = {
        int(r[0]): (int(r[1]), int(r[2])) for r in in_rows
    }

    # ── message_attachments: attachment count ─────────────────────────────
    att_rows = conn.execute(
        f"""
        SELECT m.author_id,
            SUM(CASE WHEN m.ts < ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN m.ts >= ? THEN 1 ELSE 0 END)
        FROM message_attachments a
        JOIN messages m ON a.message_id = m.message_id
        WHERE m.guild_id = ? AND m.ts >= ? AND m.ts < ?
              AND m.author_id IN ({ph})
        GROUP BY m.author_id
        """,
        [mid, mid, guild_id, start, now_ts] + user_ids,
    ).fetchall()
    att_map: dict[int, tuple[int, int]] = {
        int(r[0]): (int(r[1]), int(r[2])) for r in att_rows
    }

    # ── message_reactions: reactions received ─────────────────────────────
    react_rows = conn.execute(
        f"""
        SELECT m.author_id,
            SUM(CASE WHEN m.ts < ? THEN r.count ELSE 0 END),
            SUM(CASE WHEN m.ts >= ? THEN r.count ELSE 0 END)
        FROM message_reactions r
        JOIN messages m ON r.message_id = m.message_id
        WHERE m.guild_id = ? AND m.ts >= ? AND m.ts < ?
              AND m.author_id IN ({ph})
        GROUP BY m.author_id
        """,
        [mid, mid, guild_id, start, now_ts] + user_ids,
    ).fetchall()
    react_map: dict[int, tuple[int, int]] = {
        int(r[0]): (int(r[1]), int(r[2])) for r in react_rows
    }

    # ── messages: peak posting hour ───────────────────────────────────────
    hour_rows = conn.execute(
        f"""
        SELECT author_id,
            CAST(strftime('%H', datetime(ts, 'unixepoch')) AS INTEGER) AS hr,
            CASE WHEN ts < ? THEN 0 ELSE 1 END AS half,
            COUNT(*) AS cnt
        FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND author_id IN ({ph})
        GROUP BY author_id, half, hr
        """,
        [mid, guild_id, start, now_ts] + user_ids,
    ).fetchall()
    hour_counts: dict[tuple[int, int], dict[int, int]] = {}
    for r in hour_rows:
        key = (int(r[0]), int(r[2]))
        hour_counts.setdefault(key, {})[int(r[1])] = int(r[3])
    peak_map: dict[int, dict[int, int | None]] = {}
    for (uid, half), hc in hour_counts.items():
        peak_map.setdefault(uid, {})[half] = max(hc, key=lambda h: hc[h])

    # ── messages: longest silence gap (recent window only) ────────────────
    gap_rows = conn.execute(
        f"""
        SELECT author_id, ts FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND author_id IN ({ph})
        ORDER BY author_id, ts
        """,
        [guild_id, mid, now_ts] + user_ids,
    ).fetchall()
    gap_map: dict[int, float] = {}
    cur_uid: int | None = None
    prev_gap_ts = 0.0
    max_gap = 0.0
    for r in gap_rows:
        uid, ts = int(r[0]), float(r[1])
        if uid != cur_uid:
            if cur_uid is not None:
                gap_map[cur_uid] = max_gap
            cur_uid = uid
            prev_gap_ts = ts
            max_gap = 0.0
        else:
            g = ts - prev_gap_ts
            if g > max_gap:
                max_gap = g
            prev_gap_ts = ts
    if cur_uid is not None:
        gap_map[cur_uid] = max_gap

    # ── member_activity: last seen ────────────────────────────────────────
    last_rows = conn.execute(
        f"""
        SELECT user_id, last_message_at FROM member_activity
        WHERE guild_id = ? AND user_id IN ({ph})
        """,
        [guild_id] + user_ids,
    ).fetchall()
    last_map: dict[int, float] = {int(r[0]): float(r[1]) for r in last_rows}

    # ── channel migration (per-user channel sets per window) ──────────────
    ch_rows = conn.execute(
        f"""
        SELECT author_id, channel_id,
            SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END) AS prev_n,
            SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS recent_n
        FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND author_id IN ({ph})
        GROUP BY author_id, channel_id
        """,
        [mid, mid, guild_id, start, now_ts] + user_ids,
    ).fetchall()
    ch_migration: dict[int, tuple[list[int], list[int], list[int]]] = {}
    ch_per_user: dict[int, list[tuple[int, int, int]]] = {}
    for r in ch_rows:
        uid = int(r[0])
        ch_per_user.setdefault(uid, []).append((int(r[1]), int(r[2]), int(r[3])))
    for uid, entries in ch_per_user.items():
        left = [cid for cid, pn, rn in entries if pn > 0 and rn == 0]
        joined = [cid for cid, pn, rn in entries if pn == 0 and rn > 0]
        stayed = [cid for cid, pn, rn in entries if pn > 0 and rn > 0]
        ch_migration[uid] = (left, joined, stayed)

    # ── conversation depth (reply chains ≥3 the user participated in) ─────
    # Fetch reply edges in the window for candidate users' channels
    chain_rows = conn.execute(
        f"""
        SELECT message_id, author_id, reply_to_id,
            CASE WHEN ts < ? THEN 0 ELSE 1 END AS half
        FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND reply_to_id IS NOT NULL
              AND author_id IN ({ph})
        ORDER BY ts
        """,
        [mid, guild_id, start, now_ts] + user_ids,
    ).fetchall()
    # For each user reply, walk the reply_to chain upward to measure depth
    msg_reply: dict[int, int] = {}  # message_id → reply_to_id (for chain walking)
    # Also collect all reply_to_ids from the full window to build the chain map
    all_reply_rows = conn.execute(
        """
        SELECT message_id, reply_to_id FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ? AND reply_to_id IS NOT NULL
        """,
        [guild_id, start, now_ts],
    ).fetchall()
    for r in all_reply_rows:
        msg_reply[int(r[0])] = int(r[1])

    deep_map: dict[int, tuple[int, int]] = {}  # uid → (prev_count, recent_count)
    for r in chain_rows:
        _, uid, reply_to, half = int(r[0]), int(r[1]), int(r[2]), int(r[3])
        # Walk up the chain to count depth
        depth = 1
        cursor = reply_to
        while cursor in msg_reply and depth < 20:
            depth += 1
            cursor = msg_reply[cursor]
        if depth >= 3:
            prev_d, recent_d = deep_map.get(uid, (0, 0))
            if half == 0:
                deep_map[uid] = (prev_d + 1, recent_d)
            else:
                deep_map[uid] = (prev_d, recent_d + 1)

    # ── first activity timing (days into recent window) ───────────────────
    first_rows = conn.execute(
        f"""
        SELECT author_id, MIN(ts) FROM messages
        WHERE guild_id = ? AND ts >= ? AND ts < ?
              AND author_id IN ({ph})
        GROUP BY author_id
        """,
        [guild_id, mid, now_ts] + user_ids,
    ).fetchall()
    first_map: dict[int, int | None] = {}
    for r in first_rows:
        first_ts = float(r[1])
        days_in = int((first_ts - mid) / 86400)
        first_map[int(r[0])] = days_in

    # ── assemble profiles ─────────────────────────────────────────────────
    profiles: list[DropoffProfile] = []
    for uid in user_ids:
        mp, mr = msg_map.get(uid, (0, 0))
        md = msg_data.get(uid, {})
        xp = xp_map.get(uid, {})
        dp, dr = days_map.get(uid, (0, 0))
        om = out_map.get(uid, (0, 0, 0, 0))
        ip, ir_ = in_map.get(uid, (0, 0))
        ap, ar = att_map.get(uid, (0, 0))
        rp, rr = react_map.get(uid, (0, 0))
        peaks = peak_map.get(uid, {})
        lv, txp = level_map.get(uid, (0, 0.0))
        left, joined, stayed = ch_migration.get(uid, ([], [], []))
        dd_p, dd_r = deep_map.get(uid, (0, 0))

        voice_p, voice_r = xp.get("voice", (0.0, 0.0))
        text_p, text_r = xp.get("text", (0.0, 0.0))
        reply_xp_p, reply_xp_r = xp.get("reply", (0.0, 0.0))
        img_p, img_r = xp.get("image_react", (0.0, 0.0))

        profiles.append(
            DropoffProfile(
                user_id=uid,
                msgs_prev=mp,
                msgs_recent=mr,
                voice_xp_prev=voice_p,
                voice_xp_recent=voice_r,
                days_prev=dp,
                days_recent=dr,
                days_in_window=days_in_window,
                channels_prev=md.get("ch_p", 0),
                channels_recent=md.get("ch_r", 0),
                replies_prev=md.get("re_p", 0),
                replies_recent=md.get("re_r", 0),
                initiations_prev=md.get("in_p", 0),
                initiations_recent=md.get("in_r", 0),
                avg_len_prev=md.get("al_p", 0.0),
                avg_len_recent=md.get("al_r", 0.0),
                partners_prev=om[0],
                partners_recent=om[1],
                inbound_prev=ip,
                inbound_recent=ir_,
                outbound_prev=om[2],
                outbound_recent=om[3],
                attachments_prev=ap,
                attachments_recent=ar,
                reactions_prev=rp,
                reactions_recent=rr,
                peak_hour_prev=peaks.get(0),
                peak_hour_recent=peaks.get(1),
                weekday_pct_prev=md.get("wd_p", 0.0),
                weekday_pct_recent=md.get("wd_r", 0.0),
                longest_gap_secs=gap_map.get(uid, 0.0),
                last_seen_ts=last_map.get(uid),
                text_xp_prev=text_p,
                text_xp_recent=text_r,
                reply_xp_prev=reply_xp_p,
                reply_xp_recent=reply_xp_r,
                image_react_xp_prev=img_p,
                image_react_xp_recent=img_r,
                level=lv,
                total_xp=txp,
                channels_left=left,
                channels_joined=joined,
                channels_stayed=stayed,
                deep_convos_prev=dd_p,
                deep_convos_recent=dd_r,
                first_activity_day=first_map.get(uid),
                server_msgs_prev=srv_prev,
                server_msgs_recent=srv_recent,
            )
        )

    return profiles


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------


@_serialized_render
def render_level_histogram(
    durations_seconds: list[float],
    target_level: int,
    xp_required: float,
    mean_s: float,
    stddev_s: float,
    modal_days: int,
) -> bytes:
    """Render a histogram of time-to-reach-level durations as PNG bytes."""
    days = [s / 86400.0 for s in durations_seconds]
    mean_d = mean_s / 86400.0
    stddev_d = stddev_s / 86400.0

    max_day = max(int(d) for d in days)
    bins = list(range(0, max_day + 2))  # 1-day-wide bins

    fig, ax = plt.subplots(figsize=(10, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    ax.hist(days, bins=bins, color=_BAR, edgecolor=_BG, linewidth=0.5, zorder=2)

    # Mean ± 1 std dev band
    ax.axvspan(
        max(0.0, mean_d - stddev_d),
        mean_d + stddev_d,
        alpha=0.15,
        color="#fee75c",
        zorder=1,
    )
    ax.axvline(
        mean_d,
        color="#fee75c",
        linewidth=2,
        linestyle="--",
        label=f"Mean {mean_d:.1f}d  ±{stddev_d:.1f}d",
        zorder=3,
    )
    ax.axvline(
        modal_days + 0.5,
        color=_BAR_ACCENT,
        linewidth=2,
        linestyle=":",
        label=f"Mode {modal_days}d",
        zorder=3,
    )

    ax.set_xlabel("Days to reach level", color=_TEXT, fontsize=9)
    ax.set_ylabel("Members", color=_TEXT, fontsize=9)
    ax.set_title(
        f"Time to Reach Level {target_level}  ({xp_required:.0f} XP required)"
        f"  ·  n = {len(durations_seconds)}",
        color=_TEXT,
        fontsize=13,
        pad=10,
    )

    ax.tick_params(axis="both", colors=_TEXT, labelsize=8, length=0)
    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax.set_axisbelow(True)

    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.legend(facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT, fontsize=9)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


@_serialized_render
def render_activity_chart(
    labels: list[str],
    msg_counts: list[int] | list[float],
    member_counts: list[int],
    title: str,
    resolution: Resolution,
    *,
    show_members: bool = True,
    y_label: str = "Messages",
    bar_label: str = "Messages",
    by_source: dict[str, list[float]] | None = None,
) -> bytes:
    """
    Render an activity bar chart (values + unique members overlay) as PNG bytes.

    show_members is ignored when a specific member is being graphed.
    When ``by_source`` is provided and non-empty, bars are stacked per source
    using the dashboard XP palette, ignoring ``msg_counts``/``bar_label`` for
    bar drawing (the totals still drive y-axis auto-scaling).
    """
    n = len(labels)
    fig_width = max(9, n * 0.42)

    fig, ax1 = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax1.set_facecolor(_BG)

    x = list(range(n))
    has_breakdown = bool(by_source) and any(any(v) for v in (by_source or {}).values())
    if has_breakdown:
        assert by_source is not None
        ordered = [s for s in _XP_SOURCE_ORDER if s in by_source]
        ordered += [s for s in by_source if s not in _XP_SOURCE_ORDER]
        bottoms = [0.0] * n
        for src in ordered:
            values = by_source[src]
            if not any(values):
                continue
            color = _XP_SOURCE_COLORS.get(src, _XP_SOURCE_FALLBACK)
            label = _XP_SOURCE_LABELS.get(src, src)
            ax1.bar(
                x,
                values,
                bottom=bottoms,
                color=color,
                width=0.75,
                zorder=2,
                label=label,
            )
            bottoms = [b + v for b, v in zip(bottoms, values)]
    else:
        ax1.bar(x, msg_counts, color=_BAR, width=0.75, zorder=2, label=bar_label)

    # Unique member overlay line (server-wide only)
    if show_members and any(c > 0 for c in member_counts):
        ax2 = ax1.twinx()
        ax2.set_facecolor(_BG)
        ax2.plot(
            x,
            member_counts,
            color=_BAR_ACCENT,
            linewidth=2,
            marker="o",
            markersize=3,
            zorder=3,
            label="Unique members",
        )
        ax2.set_ylabel("Unique Members", color=_BAR_ACCENT, fontsize=9)
        ax2.tick_params(axis="y", colors=_BAR_ACCENT, labelsize=8)
        for spine in ax2.spines.values():
            spine.set_visible(False)
        ax2.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # X-axis label thinning for dense resolutions
    max_visible = 20
    if n > max_visible:
        step = max(1, n // max_visible)
        tick_positions = list(range(0, n, step))
        tick_labels_visible = [labels[i] for i in tick_positions]
    else:
        tick_positions = x
        tick_labels_visible = labels

    ax1.set_xticks(tick_positions)
    ax1.set_xticklabels(
        tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8
    )
    ax1.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax1.tick_params(length=0)

    ax1.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax1.set_axisbelow(True)

    ax1.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax1.set_ylabel(y_label, color=_TEXT, fontsize=9)
    is_int_data = all(
        isinstance(v, int) or (isinstance(v, float) and v == int(v)) for v in msg_counts
    )
    if is_int_data:
        ax1.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for spine in ax1.spines.values():
        spine.set_visible(False)

    if has_breakdown:
        ax1.legend(
            facecolor=_BG,
            edgecolor=_GRID,
            labelcolor=_TEXT,
            fontsize=8,
            loc="upper left",
            framealpha=0.85,
        )

    plt.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Period overlay, rendered (the moderator stats panel's image)
# ---------------------------------------------------------------------------
#
# The dashboard draws the overlay with Chart.js; this draws the same shape as a
# PNG for the sticky panel in a mod channel, which cannot host a live chart.
# The palette is deliberately the dashboard's, validated in
# docs/plans/weekly-activity-comparison.md against a dark surface: amber for the
# period in progress, teal for the band it is read against, and a dashed median
# so identity is never carried by colour alone.

_OVERLAY_CURRENT = "#B58030"   # amber — the subject
_OVERLAY_BAND = "#00A29C"      # teal — the comparison
_PRESENCE = "#9D79C3"          # orchid — who was watching


@dataclass(frozen=True)
class OverlayChart:
    """One overlay's worth of drawable series, plus the words around it."""

    title: str
    labels: list[str]
    current: list[float | None]
    band_low: list[float]
    band_mid: list[float]
    band_high: list[float]
    current_label: str = "Today"
    band_label: str = "Typical day"
    #: Said in place of the band when there is not enough history for one.
    empty_note: str = ""


@_serialized_render
def render_overlay_panel(
    charts: Sequence[OverlayChart],
    *,
    y_label: str = "Messages",
    x_label: str = "Hour of day",
) -> bytes:
    """Render one or more overlays stacked into a single PNG.

    One image rather than one per chart because Discord gives an embed a single
    image slot: stacking them here is what lets the reader drop their eye
    straight from one band to the next on a shared x-axis, instead of comparing
    two separately-scaled pictures Discord laid out on its own terms.

    Each chart keeps its **own** y-axis. Sharing one would let the wider band
    set the scale for both and flatten the tighter one, which is the comparison
    the panel exists to make.
    """
    if not charts:
        raise ValueError("render_overlay_panel needs at least one chart")

    rows = len(charts)
    fig, axes = plt.subplots(
        rows, 1, figsize=(9, 3.3 * rows), sharex=True, squeeze=False
    )
    fig.patch.set_facecolor(_BG)

    n = max(len(chart.labels) for chart in charts)
    x = list(range(n))

    for index, (ax, chart) in enumerate(zip((row[0] for row in axes), charts)):
        ax.set_facecolor(_BG)

        if chart.band_mid:
            ax.fill_between(
                x[: len(chart.band_low)],
                chart.band_low,
                chart.band_high,
                color=_OVERLAY_BAND,
                alpha=0.18,
                linewidth=0,
                zorder=1,
                label=f"{chart.band_label} (middle half)",
            )
            ax.plot(
                x[: len(chart.band_mid)],
                chart.band_mid,
                color=_OVERLAY_BAND,
                linewidth=1.6,
                linestyle="--",
                zorder=2,
                label=f"{chart.band_label} (median)",
            )

        # Matplotlib leaves None unplotted the same way Chart.js does, so the
        # line simply stops at the hour in progress instead of diving to the
        # floor across hours nobody has lived yet.
        ax.plot(
            x[: len(chart.current)],
            chart.current,
            color=_OVERLAY_CURRENT,
            linewidth=2.2,
            zorder=3,
            label=chart.current_label,
        )

        ax.set_title(chart.title, color=_TEXT, fontsize=11, pad=8, loc="left")
        ax.set_ylabel(y_label, color=_TEXT, fontsize=9)
        ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
        ax.tick_params(length=0)
        ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
        for spine in ax.spines.values():
            spine.set_visible(False)

        # Legended once, on the top chart. Every chart here draws the same three
        # marks with the same meanings, so a second identical box is clutter
        # sitting on top of the data it is explaining.
        if chart.band_mid and index == 0:
            ax.legend(
                facecolor=_BG,
                edgecolor=_GRID,
                labelcolor=_TEXT,
                fontsize=8,
                loc="upper left",
                framealpha=0.85,
            )
        elif not chart.band_mid and chart.empty_note:
            # Nothing to legend, so the space carries the reason instead. A
            # blank upper-left corner would read as "quiet", not as "no history
            # to compare against yet".
            ax.text(
                0.01,
                0.95,
                chart.empty_note,
                transform=ax.transAxes,
                color=_TEXT,
                fontsize=8,
                va="top",
                alpha=0.75,
            )

    # Every third hour: 24 ticks on a 9-inch axis collide, and the reader is
    # locating a time of day rather than reading a value off a gridline.
    step = 3 if n > 12 else 1
    positions = list(range(0, n, step))
    bottom = axes[-1][0]
    labels = charts[-1].labels
    bottom.set_xticks(positions)
    bottom.set_xticklabels(
        [labels[i] for i in positions], rotation=45, ha="right",
        color=_TEXT, fontsize=8,
    )
    bottom.set_xlabel(x_label, color=_TEXT, fontsize=9)
    bottom.set_xlim(0, n - 1)

    plt.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()



@dataclass(frozen=True)
class PresenceSeries:
    """Distinct moderators present in each hour of today.

    Drawn as a second line *on* the overlay rather than as a row of its own, so
    "was anyone around when it was busy?" is one glance at one picture.

    ``None`` past the hour in progress, exactly as ``OverlayChart.current`` is,
    so the two lines stop at the same place instead of one of them diving to the
    floor across hours nobody has lived yet.
    """

    values: list[int | None]
    #: Said in place of the line when the guild has no moderator role set.
    empty_note: str = ""

    @property
    def has_data(self) -> bool:
        return any(v for v in self.values)

    @property
    def peak(self) -> int:
        return max((v for v in self.values if v is not None), default=0)


@dataclass(frozen=True)
class StackedBarChart:
    """One stacked-bar row: a series per XP source, aligned to *labels*."""

    title: str
    labels: list[str]
    #: ``(source key, values)`` in draw order, bottom of the stack first.
    series: list[tuple[str, list[float]]]
    y_label: str = "XP"
    #: A line under the title, for things the bars cannot say themselves.
    note: str = ""
    #: ``source key -> bucket index`` where that source first paid out. Drawn as
    #: a dotted rule in the source's own colour.
    starts: dict[str, int] = field(default_factory=dict)


def _thin(count: int, target: int = 8) -> list[int]:
    """Tick positions for *count* bars, at most roughly *target* of them.

    Phone-first: the panel is read in an image Discord renders about 400px wide,
    where thirty date labels are a grey smear. The reader is locating a week,
    not reading a value off a gridline.
    """
    if count <= 0:
        return []
    step = max(1, math.ceil(count / target))
    return list(range(0, count, step))


@_serialized_render
def render_mod_stats_panel(
    overlay: OverlayChart,
    presence: PresenceSeries | None,
    stacks: Sequence[StackedBarChart],
) -> bytes:
    """The moderator stats panel's single PNG: overlay, presence, XP stacks.

    Separate from :func:`render_overlay_panel` because that one shares an x-axis
    across every row — right for two hour-of-day overlays, wrong the moment a
    date-bucketed bar chart joins them.

    **Mod presence rides on the overlay as a second line, sharing its zero
    baseline — not a second y-axis.** A dual-scale chart lets the author put any
    two lines into any relationship just by choosing the scales, so its crossing
    points carry a meaning nobody put there. The line is rescaled to fit and the
    scaling is named in the legend, so what the reader is invited to read off it
    is the shape — when were mods around against when was it busy — while the
    magnitudes are printed as words above the picture.

    Sized for a phone. The figure is deliberately **narrow** (6in) rather than
    short: Discord scales an embed image to the message column, so the width is
    fixed at about 400px whatever we render, and only the ratio of type size to
    figure width survives that. 11pt on 6in lands at ~10px on the phone; the
    9in-wide 8pt this panel used before landed at ~5px. Height is free — the
    reader can scroll — so rows are given room instead of being compressed.
    """
    rows: list[str] = ["overlay"]
    heights: list[float] = [3.0]
    for _ in stacks:
        rows.append("stack")
        heights.append(2.5)

    fig, axes = plt.subplots(
        len(rows),
        1,
        figsize=(6.0, sum(heights)),
        gridspec_kw={"height_ratios": heights},
        squeeze=False,
    )
    fig.patch.set_facecolor(_BG)
    flat = [row[0] for row in axes]

    def _dress(ax) -> None:
        # Note what is *not* here: set_ylim. Pinning the floor before anything
        # is drawn switches autoscaling off, and every axis then stays at
        # matplotlib's empty 0-1 default while the data sails off the top. The
        # floor is set after each row draws, not here.
        ax.set_facecolor(_BG)
        ax.tick_params(axis="y", colors=_TEXT, labelsize=10)
        ax.tick_params(length=0)
        ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)

    def _title(ax, text: str, note: str = "") -> None:
        ax.set_title(text, color=_TEXT, fontsize=13, pad=14 if note else 8, loc="left")
        if note:
            ax.text(
                0.0,
                1.02,
                note,
                transform=ax.transAxes,
                color=_TEXT,
                fontsize=9,
                alpha=0.7,
                va="bottom",
            )

    index = 0

    # ── row 1: today against its band ────────────────────────────────────
    ax = flat[index]
    index += 1
    _dress(ax)
    n_hours = len(overlay.labels)
    x = list(range(n_hours))
    if overlay.band_mid:
        ax.fill_between(
            x[: len(overlay.band_low)],
            overlay.band_low,
            overlay.band_high,
            color=_OVERLAY_BAND,
            alpha=0.18,
            linewidth=0,
            zorder=1,
            label=f"{overlay.band_label} (middle half)",
        )
        ax.plot(
            x[: len(overlay.band_mid)],
            overlay.band_mid,
            color=_OVERLAY_BAND,
            linewidth=1.6,
            linestyle="--",
            zorder=2,
            label=f"{overlay.band_label} (median)",
        )
    ax.plot(
        x[: len(overlay.current)],
        overlay.current,
        color=_OVERLAY_CURRENT,
        linewidth=2.2,
        zorder=3,
        label=overlay.current_label,
    )
    # ── mod presence, as a second line on the same axes ──────────────────
    #
    # Same zero baseline, **not** a second y-axis. A dual-scale chart lets the
    # author put any two series into any relationship just by choosing the
    # scales, so its crossing points carry a meaning nobody put there. But
    # moderators peak at a handful an hour against hundreds of messages, so an
    # unscaled line would lie flat on the floor and say nothing.
    #
    # So the line is rescaled to share the axis and the factor is *stated in
    # the legend*. The reader is being shown a shape — when were mods around,
    # against when was it busy — and the magnitudes are printed as words in the
    # block above the picture, where they need no scale at all.
    if presence is not None and presence.has_data:
        peak = presence.peak
        ceiling = max(
            (v for v in overlay.current if v is not None),
            default=0.0,
        )
        band_ceiling = max(overlay.band_high, default=0.0)
        ceiling = max(ceiling, band_ceiling)
        factor = (ceiling * 0.7 / peak) if peak and ceiling else 1.0
        ax.plot(
            list(range(len(presence.values))),
            [None if v is None else v * factor for v in presence.values],
            color=_PRESENCE,
            linewidth=1.8,
            linestyle="-",
            marker="o",
            markersize=3,
            zorder=4,
            label=f"Mods around (0-{peak}, rescaled)",
        )
    elif presence is not None and presence.empty_note:
        ax.text(
            0.01,
            0.02,
            presence.empty_note,
            transform=ax.transAxes,
            color=_TEXT,
            fontsize=9,
            va="bottom",
            alpha=0.75,
        )

    _title(ax, overlay.title)
    ax.set_ylabel("Messages", color=_TEXT, fontsize=10)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True, nbins=5))
    ax.set_ylim(bottom=0)
    ax.set_xlim(0, max(1, n_hours - 1))
    positions = list(range(0, n_hours, 3))
    ax.set_xticks(positions)
    ax.set_xticklabels(
        [overlay.labels[i] for i in positions], color=_TEXT, fontsize=10
    )
    ax.set_xlabel("Hour of day", color=_TEXT, fontsize=10)
    if overlay.band_mid or (presence is not None and presence.has_data):
        ax.legend(
            facecolor=_BG,
            edgecolor=_GRID,
            labelcolor=_TEXT,
            fontsize=9,
            loc="upper left",
            framealpha=0.85,
        )
    elif overlay.empty_note:
        ax.text(
            0.01,
            0.95,
            overlay.empty_note,
            transform=ax.transAxes,
            color=_TEXT,
            fontsize=9,
            va="top",
            alpha=0.75,
        )

    # ── the XP stacks ────────────────────────────────────────────────────
    # BarContainer, which matplotlib's legend takes as a handle but which is
    # not an Artist subclass, so it cannot be spelled more tightly than this.
    seen: dict[str, Any] = {}
    for stack in stacks:
        ax = flat[index]
        index += 1
        _dress(ax)
        count = len(stack.labels)
        xs = list(range(count))
        bottoms = [0.0] * count
        for source, values in stack.series:
            if not any(values):
                continue
            color = _XP_SOURCE_COLORS.get(source, _XP_SOURCE_FALLBACK)
            label = _XP_SOURCE_LABELS.get(source, source)
            # edgecolor=_BG is the 2px surface gap the shared palette's one weak
            # pair depends on — the secondary encoding that makes teal/orchid
            # legal where they share an edge. It is load-bearing, not decorative.
            bars = ax.bar(
                xs,
                values,
                bottom=bottoms,
                color=color,
                width=0.8,
                zorder=2,
                edgecolor=_BG,
                linewidth=0.8,
                label=label,
            )
            seen.setdefault(label, bars)
            bottoms = [b + v for b, v in zip(bottoms, values)]

        for source, at in sorted(stack.starts.items(), key=lambda kv: kv[1]):
            if at <= 0:
                continue
            ax.axvline(
                at - 0.5,
                color=_XP_SOURCE_COLORS.get(source, _XP_SOURCE_FALLBACK),
                linestyle=":",
                linewidth=1.4,
                alpha=0.9,
                zorder=4,
            )

        _title(ax, stack.title, stack.note)
        ax.set_ylabel(stack.y_label, color=_TEXT, fontsize=10)
        ax.set_ylim(bottom=0)
        positions = _thin(count)
        ax.set_xticks(positions)
        ax.set_xticklabels(
            [stack.labels[i] for i in positions],
            rotation=45,
            ha="right",
            color=_TEXT,
            fontsize=10,
        )
        ax.set_xlim(-0.6, max(0.6, count - 0.4))

    # One legend for both stacks rather than one each: they draw the same
    # sources with the same meanings, and a second identical box on a phone is
    # a whole row of chart lost to saying it twice.
    if seen:
        fig.legend(
            list(seen.values()),
            list(seen.keys()),
            facecolor=_BG,
            edgecolor=_GRID,
            labelcolor=_TEXT,
            fontsize=9,
            loc="lower center",
            ncol=3,
            framealpha=0.9,
            bbox_to_anchor=(0.5, -0.01),
        )

    fig.tight_layout(pad=1.1, h_pad=2.0, rect=(0, 0.05 if seen else 0, 1, 1))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Role growth over time
# ---------------------------------------------------------------------------

_ROLE_COLORS = [
    "#5865f2",  # blurple
    "#eb459e",  # pink
    "#fee75c",  # yellow
    "#57f287",  # green
    "#ed4245",  # red
    "#9b84ec",  # purple
]


# ---------------------------------------------------------------------------
# Session burst profile
# ---------------------------------------------------------------------------

_IDLE_THRESHOLD_SECONDS = 20 * 60  # 20 minutes defines a session boundary
_PRE_WINDOW_MINUTES = 20
_POST_WINDOW_MINUTES = 60
_BIN_MINUTES = 2


# ---------------------------------------------------------------------------
# Burst ranking — highest / lowest burst increase across all users
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Message cadence — inter-message time stats over time
# ---------------------------------------------------------------------------


@dataclass
class CadenceBucket:
    label: str
    min_gap: float  # wick low
    p20_gap: float  # body low (open)
    median_gap: float  # body mid
    p80_gap: float  # body high (close)
    max_gap: float  # wick high


# ---------------------------------------------------------------------------
# Member join histogram
# ---------------------------------------------------------------------------


@_serialized_render
def render_join_histogram(
    labels: list[str],
    counts: list[int],
    title: str,
) -> bytes:
    """Render a bar chart of member join counts per bucket."""
    n = len(labels)
    fig_width = max(9, n * 0.42)
    fig, ax = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    x = list(range(n))
    ax.bar(x, counts, color=_BAR, width=0.75, zorder=2)

    max_visible = 20
    if n > max_visible:
        step = max(1, n // max_visible)
        tick_positions = list(range(0, n, step))
        tick_labels_visible = [labels[i] for i in tick_positions]
    else:
        tick_positions = x
        tick_labels_visible = labels

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8
    )
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax.tick_params(length=0)

    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax.set_ylabel("Members joined", color=_TEXT, fontsize=9)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for spine in ax.spines.values():
        spine.set_visible(False)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# NSFW posting by gender
# ---------------------------------------------------------------------------

_GENDER_COLORS = {
    "male": "#5865f2",  # blurple
    "female": "#eb459e",  # pink
    "nonbinary": "#57f287",  # green
    "unknown": "#72767d",  # gray
}

_GENDER_ORDER = ["male", "female", "nonbinary", "unknown"]


def query_nsfw_gender_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    channel_ids: list[int],
    *,
    utc_offset_hours: float = 0,
    media_only: bool = False,
    include_bots: bool = False,
) -> tuple[list[str], dict[str, list[int]]]:
    """
    Query channel message counts per time bucket, grouped by gender.

    Returns (labels, {gender: [count_per_bucket]}).
    Members without a gender classification are bucketed as 'unknown'.

    When *media_only* is True, only messages with image/video attachments
    (excluding GIFs) are counted.  This joins the ``messages`` and
    ``message_attachments`` tables instead of ``processed_messages``.
    """
    if not channel_ids:
        return [], {}

    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now, utc_offset_hours)
    offset_secs = int(utc_offset_hours * 3600)

    ch_placeholders = ", ".join("?" for _ in channel_ids)
    params: list[object] = [guild_id, since_ts, *channel_ids]

    bucket_expr = _strftime_expr(
        resolution, col="m.ts", since_ts=since_ts, utc_offset_secs=offset_secs
    )
    # ``media_kind`` is recorded at ingest as lightweight metadata (an attachment
    # classification, not a URL), so the media split works even at storage level
    # "none". 'media' = non-gif image/video — exactly what this metric counts.
    media_filter = "AND m.media_kind = 'media'" if media_only else ""
    # LEFT JOIN, so bot authors are not dropped by the gender join — they fall
    # into the 'unknown' bucket and inflate it. Filter them explicitly.
    bot_clause, bot_params = bot_filter_clause(
        guild_id, column="m.author_id", include_bots=include_bots
    )
    params.extend(bot_params)
    rows = conn.execute(
        f"""
        SELECT
            {bucket_expr} AS bucket,
            COALESCE(mg.gender, 'unknown') AS gender,
            COUNT(*) AS cnt
        FROM messages m
        LEFT JOIN member_gender mg
            ON mg.guild_id = m.guild_id AND mg.user_id = m.author_id
        WHERE m.guild_id = ? AND m.ts >= ?
            AND m.channel_id IN ({ch_placeholders})
            {media_filter}{bot_clause}
        GROUP BY bucket, gender
        """,
        params,
    ).fetchall()

    # Build per-gender counts aligned to bucket sequence
    counts_by_gender: dict[str, dict[str, int]] = {}
    for r in rows:
        g = str(r["gender"])
        counts_by_gender.setdefault(g, {})[str(r["bucket"])] = int(r["cnt"])

    labels = [label for _, label in bucket_sequence]
    gender_counts: dict[str, list[int]] = {}
    for g in _GENDER_ORDER:
        if g not in counts_by_gender:
            continue
        gender_counts[g] = [
            counts_by_gender[g].get(key, 0) for key, _ in bucket_sequence
        ]

    return labels, gender_counts


#: Display order for the tagger's vocabulary, and — via each label's *position
#: in this list* — the palette slot it is drawn in.  Taxonomic rather than by
#: frequency, so the two chest labels and the two genitalia labels sit next to
#: their counterpart and a reader can compare them without hunting across the
#: legend.
#:
#: The position is what makes a series' colour stable.  Colouring by the index
#: within a *result* would repaint half the chart whenever a narrower window or
#: a channel filter dropped one label out of the middle: BUTTOCKS_EXPOSED moves
#: from slot 4 to slot 3 and changes hue, having done nothing.  So the emitted
#: series carries its taxonomy index and the panel colours from that, not from
#: its own enumeration.
#:
#: ANUS_EXPOSED is last because the palette has six slots and this list has
#: seven entries, so whatever sits at the end is drawn in the overflow neutral.
#: It is the one label in the vocabulary the detector has never once emitted in
#: production (0 rows against 682 tagged), which makes it the honest one to put
#: where the colours run out. If it ever starts appearing, the tail wants
#: folding into an "Other" band instead — see the note on SERIES_OVERFLOW in
#: charts.js.
#:
#: Mirrors ``nsfw_classifier_service.DEFAULT_LABEL_SET``, which is a frozenset
#: and so cannot supply an order itself; a drift test pins the two together,
#: because a new label appended by the detector would otherwise land at the
#: tail and silently shift every colour after it.  A label missing from here is
#: still reported (appended, sorted) rather than dropped — the vocabulary is
#: the detector's, not ours.
TAG_ORDER = [
    "FEMALE_BREAST_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "SEX_ACT",
    "ANUS_EXPOSED",
]


def query_nsfw_tag_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    *,
    utc_offset_hours: float = 0,
    channel_ids: list[int] | None = None,
) -> tuple[list[str], dict[str, list[int]]]:
    """
    Query tagged-image counts per time bucket, grouped by NudeNet's top label.

    Returns (labels, {top_label: [count_per_bucket]}).

    Unlike :func:`query_nsfw_gender_activity` this does **not** discover NSFW
    channels and filter to them.  ``nsfw_classifications`` only holds rows for
    channels the tagger actually ran in — age-gated ones *and* spoiler-required
    ones, which Discord need not age-gate — so the table is already scoped and
    re-deriving that scope here would silently drop the second set.
    *channel_ids* is therefore a caller's narrowing filter, never the boundary.

    Rows with no ``top_label`` are excluded rather than bucketed as 'unknown'.
    Marqo writes a verdict for every image it sees; NudeNet writes a label only
    where it ran *and* found something.  An 'unknown' band would therefore be
    dominated by images outside this report's scope entirely, which is a
    different fact from "tagged, but nothing qualified".

    Pre-swap rows (``marqo_score IS NULL``, migration 147) are excluded to match
    ``/api/moderation/nsfw-tags``.  Their *labels* are perfectly good — NudeNet
    wrote them the same way it does now, so the exclusion is not the
    two-meanings-of-explicit argument that governs the verdict columns.  It is
    consistency: the two reports describe the same table side by side and are
    documented as showing the same labels, so a total that silently disagrees
    is worse than four dropped rows.  Four, in production, all of them older
    than any window shorter than 12 months.
    """
    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now, utc_offset_hours)
    offset_secs = int(utc_offset_hours * 3600)

    bucket_expr = _strftime_expr(
        resolution, col="created_at", since_ts=since_ts, utc_offset_secs=offset_secs
    )

    params: list[object] = [guild_id, since_ts]
    channel_filter = ""
    if channel_ids:
        placeholders = ", ".join("?" for _ in channel_ids)
        channel_filter = f"AND channel_id IN ({placeholders})"
        params.extend(channel_ids)

    rows = conn.execute(
        f"""
        SELECT
            {bucket_expr} AS bucket,
            top_label AS label,
            COUNT(*) AS cnt
        FROM nsfw_classifications
        WHERE guild_id = ? AND created_at >= ?
            AND top_label IS NOT NULL AND top_label != ''
            AND marqo_score IS NOT NULL
            {channel_filter}
        GROUP BY bucket, label
        """,
        params,
    ).fetchall()

    counts_by_label: dict[str, dict[str, int]] = {}
    for r in rows:
        lbl = str(r["label"])
        counts_by_label.setdefault(lbl, {})[str(r["bucket"])] = int(r["cnt"])

    labels = [label for _, label in bucket_sequence]
    ordered = [t for t in TAG_ORDER if t in counts_by_label]
    ordered += sorted(t for t in counts_by_label if t not in TAG_ORDER)

    tag_counts: dict[str, list[int]] = {
        t: [counts_by_label[t].get(key, 0) for key, _ in bucket_sequence]
        for t in ordered
    }
    return labels, tag_counts


@_serialized_render
def render_nsfw_gender_chart(
    labels: list[str],
    gender_counts: dict[str, list[int]],
    title: str,
) -> bytes:
    """Render a stacked bar chart of NSFW posting by gender as PNG bytes."""
    import numpy as np

    n = len(labels)
    fig_width = max(9, n * 0.42)

    fig, ax = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    x = np.arange(n)
    bar_width = 0.7
    bottom = np.zeros(n)

    for gender in _GENDER_ORDER:
        if gender not in gender_counts:
            continue
        values = np.array(gender_counts[gender], dtype=float)
        color = _GENDER_COLORS.get(gender, _GENDER_COLORS["unknown"])
        ax.bar(
            x,
            values,
            bar_width,
            bottom=bottom,
            color=color,
            label=gender.capitalize(),
            zorder=2,
        )
        bottom += values

    # Smart x-axis labeling
    max_visible = 20
    if n > max_visible:
        step = max(1, n // max_visible)
        tick_positions = list(range(0, n, step))
        tick_labels_visible = [labels[i] for i in tick_positions]
    else:
        tick_positions = list(range(n))
        tick_labels_visible = labels

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8
    )
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax.tick_params(length=0)

    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax.set_ylabel("Messages", color=_TEXT, fontsize=9)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for spine in ax.spines.values():
        spine.set_visible(False)

    if gender_counts:
        ax.legend(facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT, fontsize=9)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


@_serialized_render
def render_nsfw_gender_line_chart(
    labels: list[str],
    gender_counts: dict[str, list[int]],
    title: str,
) -> bytes:
    """Render a line chart showing gender ratio over time as PNG bytes."""
    import numpy as np

    n = len(labels)
    fig_width = max(9, n * 0.42)

    fig, ax = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    x = np.arange(n)

    all_genders = [g for g in _GENDER_ORDER if g in gender_counts]
    if not all_genders:
        plt.close(fig)
        return render_nsfw_gender_chart(labels, gender_counts, title)

    stacked = np.array([gender_counts[g] for g in all_genders], dtype=float)
    totals = stacked.sum(axis=0)
    totals[totals == 0] = 1  # avoid division by zero

    for gender in _GENDER_ORDER:
        if gender not in gender_counts:
            continue
        values = np.array(gender_counts[gender], dtype=float)
        pct = values / totals * 100
        color = _GENDER_COLORS.get(gender, _GENDER_COLORS["unknown"])
        ax.plot(
            x,
            pct,
            color=color,
            linewidth=2,
            marker="o",
            markersize=4,
            label=gender.capitalize(),
            zorder=2,
        )

    max_visible = 20
    if n > max_visible:
        step = max(1, n // max_visible)
        tick_positions = list(range(0, n, step))
        tick_labels_visible = [labels[i] for i in tick_positions]
    else:
        tick_positions = list(range(n))
        tick_labels_visible = labels

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8
    )
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax.tick_params(length=0)

    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax.set_ylabel("% of Posts", color=_TEXT, fontsize=9)
    ax.set_ylim(0, 100)

    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.legend(facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT, fontsize=9)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Greeter response time
# ---------------------------------------------------------------------------

_RESPONSE_BUCKETS: list[tuple[float, str]] = [
    (60, "< 1m"),
    (300, "1\u20135m"),
    (900, "5\u201315m"),
    (1800, "15\u201330m"),
    (3600, "30\u201360m"),
    (14400, "1\u20134h"),
    (43200, "4\u201312h"),
    (86400, "12\u201324h"),
    (float("inf"), "> 24h"),
]


def query_greeter_response_times(
    conn: sqlite3.Connection,
    guild_id: int,
    greeter_channel_id: int,
    greeter_user_ids: set[int],
    join_times: dict[int, float],
) -> list[float]:
    """Return greeter response times in seconds for each join that received a response.

    *join_times* maps member-id -> joined-at unix timestamp.
    """
    if not greeter_user_ids or not join_times:
        return []

    placeholders = ",".join("?" * len(greeter_user_ids))
    greeter_msgs = conn.execute(
        f"""
        SELECT ts FROM messages
        WHERE guild_id = ? AND channel_id = ?
          AND author_id IN ({placeholders})
        ORDER BY ts
        """,
        (guild_id, greeter_channel_id, *greeter_user_ids),
    ).fetchall()

    greeter_times = [int(r[0]) for r in greeter_msgs]
    if not greeter_times:
        return []

    response_times: list[float] = []
    for joined_at in join_times.values():
        idx = bisect.bisect_left(greeter_times, joined_at)
        if idx < len(greeter_times):
            delta = greeter_times[idx] - joined_at
            if delta >= 0:
                response_times.append(delta)

    return response_times


@_serialized_render
def render_greeter_response_chart(
    response_times: list[float],
    title: str,
) -> bytes:
    """Render a histogram of greeter response times as PNG bytes."""
    bucket_counts = [0] * len(_RESPONSE_BUCKETS)
    for t in response_times:
        for i, (threshold, _) in enumerate(_RESPONSE_BUCKETS):
            if t < threshold:
                bucket_counts[i] += 1
                break

    labels = [label for _, label in _RESPONSE_BUCKETS]
    n = len(labels)

    fig, ax = plt.subplots(figsize=(max(9, n * 0.8), 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    x = list(range(n))
    bars = ax.bar(x, bucket_counts, color=_BAR, width=0.7, zorder=2)

    for bar, count in zip(bars, bucket_counts):
        if count > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.3,
                str(count),
                ha="center",
                va="bottom",
                color=_TEXT,
                fontsize=8,
            )

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", color=_TEXT, fontsize=9)
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax.tick_params(length=0)

    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax.set_ylabel("Joins", color=_TEXT, fontsize=9)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for spine in ax.spines.values():
        spine.set_visible(False)

    if response_times:
        med = statistics.median(response_times)
        avg = statistics.mean(response_times)

        def _fmt_dur(s: float) -> str:
            if s < 60:
                return f"{s:.0f}s"
            if s < 3600:
                return f"{s / 60:.0f}m"
            return f"{s / 3600:.1f}h"

        ax.annotate(
            f"median {_fmt_dur(med)}  \u00b7  mean {_fmt_dur(avg)}  \u00b7  n={len(response_times)}",
            xy=(0.5, 1.0),
            xycoords="axes fraction",
            ha="center",
            va="bottom",
            fontsize=9,
            color=_BAR_ACCENT,
        )

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Message rate (10-minute time-of-day buckets)
# ---------------------------------------------------------------------------


