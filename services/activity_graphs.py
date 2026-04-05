"""Activity graph generation — message counts bucketed by time resolution."""
from __future__ import annotations

import bisect
from dataclasses import dataclass
import io
import sqlite3
import statistics
from itertools import groupby
from datetime import datetime, timedelta, timezone
from typing import Literal

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

matplotlib.use("Agg")

Resolution = Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"]

_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_HOD_LABELS = [
    "12am", "1am", "2am", "3am", "4am", "5am", "6am", "7am",
    "8am", "9am", "10am", "11am", "12pm", "1pm", "2pm", "3pm",
    "4pm", "5pm", "6pm", "7pm", "8pm", "9pm", "10pm", "11pm",
]

# Discord dark theme palette
_BG = "#2f3136"
_BAR = "#5865f2"
_BAR_ACCENT = "#eb459e"  # pink for unique-members line
_TEXT = "#dcddde"
_GRID = "#40444b"


# ---------------------------------------------------------------------------
# Bucket sequence builders
# ---------------------------------------------------------------------------


def _hour_buckets(now: datetime, utc_offset_hours: float = 0) -> tuple[list[tuple[str, str]], float]:
    """24 hourly buckets ending at the current hour."""
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    start = local_now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    buckets = []
    for i in range(24):
        dt = start + timedelta(hours=i)
        key = (dt - offset).strftime("%Y-%m-%d %H")  # key in UTC for SQL match
        label = dt.strftime("%a %H:%M")               # label in local time
        buckets.append((key, label))
    return buckets, (start - offset).timestamp()


def _day_buckets(now: datetime, utc_offset_hours: float = 0) -> tuple[list[tuple[str, str]], float]:
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


def _week_buckets(now: datetime, utc_offset_hours: float = 0) -> tuple[list[tuple[str, str]], float]:
    """12 weekly buckets (Monday-based) ending this week."""
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    days_since_monday = local_now.weekday()
    this_monday = (local_now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = this_monday - timedelta(weeks=11)
    buckets = []
    for i in range(12):
        dt = start + timedelta(weeks=i)
        key = (dt - offset).strftime("%Y-%W")  # key in UTC
        label = dt.strftime("%b %d")
        buckets.append((key, label))
    return buckets, (start - offset).timestamp()


def _month_buckets(now: datetime, utc_offset_hours: float = 0) -> tuple[list[tuple[str, str]], float]:
    """12 monthly buckets ending this month."""
    offset = timedelta(hours=utc_offset_hours)
    local_now = now + offset
    buckets = []
    for i in range(11, -1, -1):
        m = local_now.month - i
        y = local_now.year
        while m <= 0:
            m += 12
            y -= 1
        dt = datetime(y, m, 1, tzinfo=timezone.utc)
        key = dt.strftime("%Y-%m")
        label = dt.strftime("%b '%y")
        buckets.append((key, label))
    first_y = int(buckets[0][0][:4])
    first_m = int(buckets[0][0][5:])
    since_ts = datetime(first_y, first_m, 1, tzinfo=timezone.utc).timestamp()
    return buckets, since_ts


def _strftime_expr(
    resolution: Resolution,
    col: str = "created_at",
    since_ts: float = 0,
    utc_offset_secs: int = 0,
) -> str:
    """SQLite expression that buckets a timestamp column into the right key format.

    For ``day`` resolution the buckets are rolling 24-hour windows anchored to
    the query start, so the key is the epoch of the bucket's upper edge.  All
    other resolutions use the traditional ``strftime`` calendar bucketing.

    *utc_offset_secs* shifts the timestamp before bucketing so that calendar
    boundaries (midnight, Monday, month start) align with the user's local time.
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
        return f"strftime('%Y-%W', datetime({shifted}, 'unixepoch'))"
    return f"strftime('%Y-%m', datetime({shifted}, 'unixepoch'))"


_BUCKET_BUILDERS = {
    "hour": _hour_buckets,
    "day": _day_buckets,
    "week": _week_buckets,
    "month": _month_buckets,
}

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


def query_message_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
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
    bucket_expr = _strftime_expr(resolution, since_ts=since_ts, utc_offset_secs=offset_secs)

    params: list[object] = [guild_id, since_ts]
    where = "guild_id = ? AND created_at >= ?"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    if channel_id is not None:
        where += " AND channel_id = ?"
        params.append(channel_id)

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
# Message-rate drop analysis
# ---------------------------------------------------------------------------


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


def query_dropoff_profiles(
    conn: sqlite3.Connection,
    guild_id: int,
    period_seconds: float,
    *,
    channel_id: int | None = None,
    min_previous: int = 5,
    limit: int = 10,
    target_user_id: int | None = None,
) -> list[DropoffProfile]:
    """Compute enriched engagement profiles for users with message-rate drops.

    If *target_user_id* is given, returns a single-element list with that user's
    profile regardless of whether they had a dropoff (useful for the detail view).
    Candidate selection honours *channel_id*; enrichment queries are server-wide.
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
            conn, guild_id, period_seconds,
            channel_id=channel_id, min_previous=min_previous, limit=limit,
        )

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
            "ch_p": int(r[1]), "ch_r": int(r[2]),
            "re_p": int(r[3]), "re_r": int(r[4]),
            "in_p": int(r[5]), "in_r": int(r[6]),
            "al_p": float(r[7] or 0), "al_r": float(r[8] or 0),
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

        profiles.append(DropoffProfile(
            user_id=uid,
            msgs_prev=mp, msgs_recent=mr,
            voice_xp_prev=voice_p, voice_xp_recent=voice_r,
            days_prev=dp, days_recent=dr, days_in_window=days_in_window,
            channels_prev=md.get("ch_p", 0), channels_recent=md.get("ch_r", 0),
            replies_prev=md.get("re_p", 0), replies_recent=md.get("re_r", 0),
            initiations_prev=md.get("in_p", 0), initiations_recent=md.get("in_r", 0),
            avg_len_prev=md.get("al_p", 0.0), avg_len_recent=md.get("al_r", 0.0),
            partners_prev=om[0], partners_recent=om[1],
            inbound_prev=ip, inbound_recent=ir_,
            outbound_prev=om[2], outbound_recent=om[3],
            attachments_prev=ap, attachments_recent=ar,
            reactions_prev=rp, reactions_recent=rr,
            peak_hour_prev=peaks.get(0), peak_hour_recent=peaks.get(1),
            weekday_pct_prev=md.get("wd_p", 0.0), weekday_pct_recent=md.get("wd_r", 0.0),
            longest_gap_secs=gap_map.get(uid, 0.0),
            last_seen_ts=last_map.get(uid),
            text_xp_prev=text_p, text_xp_recent=text_r,
            reply_xp_prev=reply_xp_p, reply_xp_recent=reply_xp_r,
            image_react_xp_prev=img_p, image_react_xp_recent=img_r,
            level=lv, total_xp=txp,
            channels_left=left, channels_joined=joined, channels_stayed=stayed,
            deep_convos_prev=dd_p, deep_convos_recent=dd_r,
            first_activity_day=first_map.get(uid),
            server_msgs_prev=srv_prev,
            server_msgs_recent=srv_recent,
        ))

    return profiles


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------


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


def render_activity_chart(
    labels: list[str],
    msg_counts: list[int],
    member_counts: list[int],
    title: str,
    resolution: Resolution,
    *,
    show_members: bool = True,
) -> bytes:
    """
    Render an activity bar chart (messages + unique members overlay) as PNG bytes.

    show_members is ignored when a specific member is being graphed.
    """
    n = len(labels)
    fig_width = max(9, n * 0.42)

    fig, ax1 = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax1.set_facecolor(_BG)

    x = list(range(n))
    ax1.bar(x, msg_counts, color=_BAR, width=0.75, zorder=2, label="Messages")

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
    ax1.set_xticklabels(tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8)
    ax1.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax1.tick_params(length=0)

    ax1.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax1.set_axisbelow(True)

    ax1.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax1.set_ylabel("Messages", color=_TEXT, fontsize=9)
    ax1.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for spine in ax1.spines.values():
        spine.set_visible(False)

    plt.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
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


def query_role_growth(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
) -> tuple[list[str], dict[str, list[int]]]:
    """
    Query cumulative role grant counts per time bucket.

    Returns (labels, {role_name: [cumulative_count_per_bucket]}).
    The cumulative count includes grants before the window start as a baseline.
    """
    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now)
    bucket_expr = _strftime_expr(resolution, col="granted_at", since_ts=since_ts)

    # Grants that happened before the window — used as per-role baselines
    baseline_rows = conn.execute(
        """
        SELECT role_name, COUNT(*) AS cnt
        FROM role_events
        WHERE guild_id = ? AND action = 'grant' AND granted_at < ?
        GROUP BY role_name
        """,
        (guild_id, since_ts),
    ).fetchall()
    baselines: dict[str, int] = {str(r[0]): int(r[1]) for r in baseline_rows}

    # Grants within the window, grouped by role and time bucket
    window_rows = conn.execute(
        f"""
        SELECT role_name, {bucket_expr} AS bucket, COUNT(*) AS cnt
        FROM role_events
        WHERE guild_id = ? AND action = 'grant' AND granted_at >= ?
        GROUP BY role_name, bucket
        """,
        (guild_id, since_ts),
    ).fetchall()

    grants_by_role: dict[str, dict[str, int]] = {}
    for r in window_rows:
        role, bucket, cnt = str(r[0]), str(r[1]), int(r[2])
        grants_by_role.setdefault(role, {})[bucket] = cnt

    all_roles = sorted(set(list(baselines.keys()) + list(grants_by_role.keys())))
    labels = [label for _, label in bucket_sequence]

    role_counts: dict[str, list[int]] = {}
    for role in all_roles:
        running = baselines.get(role, 0)
        counts: list[int] = []
        for key, _ in bucket_sequence:
            running += grants_by_role.get(role, {}).get(key, 0)
            counts.append(running)
        role_counts[role] = counts

    return labels, role_counts


def render_role_growth_chart(
    labels: list[str],
    role_counts: dict[str, list[int]],
    title: str,
) -> bytes:
    """Render a cumulative role-grant line chart as PNG bytes."""
    n = len(labels)
    fig_width = max(9, n * 0.42)

    fig, ax = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    x = list(range(n))
    for i, (role_name, counts) in enumerate(role_counts.items()):
        color = _ROLE_COLORS[i % len(_ROLE_COLORS)]
        ax.plot(x, counts, color=color, linewidth=2, marker="o", markersize=3,
                label=role_name, zorder=2)

    max_visible = 20
    if n > max_visible:
        step = max(1, n // max_visible)
        tick_positions = list(range(0, n, step))
        tick_labels_visible = [labels[i] for i in tick_positions]
    else:
        tick_positions = x
        tick_labels_visible = labels

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8)
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax.tick_params(length=0)

    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax.set_ylabel("Members", color=_TEXT, fontsize=9)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for spine in ax.spines.values():
        spine.set_visible(False)

    if role_counts:
        ax.legend(facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT, fontsize=9)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Session burst profile
# ---------------------------------------------------------------------------

_IDLE_THRESHOLD_SECONDS = 20 * 60   # 20 minutes defines a session boundary
_PRE_WINDOW_MINUTES = 20
_POST_WINDOW_MINUTES = 60
_BIN_MINUTES = 2


def query_session_burst(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> tuple[list[list[float]], list[list[float]], float]:
    """
    Find all session starts for a user and compute per-bin server message counts.

    A session starts when the user's first message follows a gap of ≥20 minutes.
    For each session, **all guild messages** are counted in 2-minute bins covering:
      - the 20 minutes before session start  (pre-bins)
      - the 60 minutes after session start   (post-bins)

    Returns:
        pre_sessions  – list of sessions, each a list of _PRE_WINDOW_MINUTES//_BIN_MINUTES counts
        post_sessions – list of sessions, each a list of _POST_WINDOW_MINUTES//_BIN_MINUTES counts
        overall_rate  – server messages per _BIN_MINUTES across the guild's full recorded history
    """
    # User timestamps — used only to detect session starts
    user_rows = conn.execute(
        """
        SELECT created_at FROM processed_messages
        WHERE guild_id = ? AND user_id = ?
        ORDER BY created_at
        """,
        (guild_id, user_id),
    ).fetchall()

    user_ts = [float(r[0]) for r in user_rows]
    if len(user_ts) < 2:
        return [], [], 0.0

    pre_bins_count = _PRE_WINDOW_MINUTES // _BIN_MINUTES
    post_bins_count = _POST_WINDOW_MINUTES // _BIN_MINUTES
    bin_secs = _BIN_MINUTES * 60
    pre_secs = _PRE_WINDOW_MINUTES * 60
    post_secs = _POST_WINDOW_MINUTES * 60

    # Find session starts (gap ≥ 20 min before this user message)
    session_starts: list[float] = [user_ts[0]]
    for i in range(1, len(user_ts)):
        if user_ts[i] - user_ts[i - 1] >= _IDLE_THRESHOLD_SECONDS:
            session_starts.append(user_ts[i])

    # Fetch all guild messages in the window that covers all sessions
    window_lo = min(session_starts) - pre_secs
    window_hi = max(session_starts) + post_secs
    guild_rows = conn.execute(
        """
        SELECT created_at FROM processed_messages
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
        ORDER BY created_at
        """,
        (guild_id, window_lo, window_hi),
    ).fetchall()
    guild_ts = [float(r[0]) for r in guild_rows]

    pre_sessions: list[list[float]] = []
    post_sessions: list[list[float]] = []

    for start_ts in session_starts:
        # Pre-window: guild messages in [start_ts - pre_secs, start_ts)
        pre_bins: list[float] = [0.0] * pre_bins_count
        lo = bisect.bisect_left(guild_ts, start_ts - pre_secs)
        hi = bisect.bisect_left(guild_ts, start_ts)
        for ts in guild_ts[lo:hi]:
            offset = ts - (start_ts - pre_secs)
            bin_i = int(offset // bin_secs)
            if 0 <= bin_i < pre_bins_count:
                pre_bins[bin_i] += 1
        pre_sessions.append(pre_bins)

        # Post-window: guild messages in [start_ts, start_ts + post_secs)
        post_bins: list[float] = [0.0] * post_bins_count
        lo = bisect.bisect_left(guild_ts, start_ts)
        for ts in guild_ts[lo:]:
            offset = ts - start_ts
            if offset >= post_secs:
                break
            bin_i = int(offset // bin_secs)
            if 0 <= bin_i < post_bins_count:
                post_bins[bin_i] += 1
        post_sessions.append(post_bins)

    # Overall rate: total guild messages over the full recorded guild history
    total_row = conn.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM processed_messages WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    total_count = int(total_row[0]) if total_row and total_row[0] else 0
    if total_count > 1:
        total_mins = (float(total_row[2]) - float(total_row[1])) / 60.0
        total_bins = max(1.0, total_mins / _BIN_MINUTES)
        overall_rate = total_count / total_bins
    else:
        overall_rate = 0.0

    return pre_sessions, post_sessions, overall_rate


def render_session_burst_chart(
    pre_sessions: list[list[float]],
    post_sessions: list[list[float]],
    overall_rate: float,
    user_display_name: str,
) -> bytes:
    """Render the average session burst profile as PNG bytes."""
    n_pre = _PRE_WINDOW_MINUTES // _BIN_MINUTES
    n_post = _POST_WINDOW_MINUTES // _BIN_MINUTES
    n_sessions = len(post_sessions)

    def _mean_bins(sessions: list[list[float]]) -> list[float]:
        if not sessions:
            return []
        n = len(sessions[0])
        return [sum(s[i] for s in sessions) / n_sessions for i in range(n)]

    mean_pre = _mean_bins(pre_sessions)
    mean_post = _mean_bins(post_sessions)

    # X positions: pre bins are negative, post bins are positive
    x_pre = [(-_PRE_WINDOW_MINUTES + i * _BIN_MINUTES + _BIN_MINUTES / 2) for i in range(n_pre)]
    x_post = [(i * _BIN_MINUTES + _BIN_MINUTES / 2) for i in range(n_post)]

    fig_width = max(11, (n_pre + n_post) * 0.45)
    fig, ax = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    # Pre-session bars (muted colour)
    ax.bar(x_pre, mean_pre, width=_BIN_MINUTES * 0.85, color="#4e5058", zorder=2, label="Server activity (pre)")
    # Post-session bars
    ax.bar(x_post, mean_post, width=_BIN_MINUTES * 0.85, color=_BAR, zorder=2, label="Server activity (post)")

    # Individual session lines (faint), capped at 20 to keep the chart readable
    if n_sessions <= 20:
        for pre, post in zip(pre_sessions, post_sessions):
            ax.plot(x_pre, pre, color="#4e5058", linewidth=0.6, alpha=0.4, zorder=1)
            ax.plot(x_post, post, color=_BAR, linewidth=0.6, alpha=0.4, zorder=1)

    # Overall average rate reference line
    ax.axhline(
        overall_rate,
        color="#fee75c",
        linewidth=1.5,
        linestyle="--",
        label=f"Server avg ({overall_rate:.2f} msg / {_BIN_MINUTES}min)",
        zorder=3,
    )

    # Session-start marker
    ax.axvline(0, color=_BAR_ACCENT, linewidth=2, linestyle="-", label="Session start", zorder=4)

    # Shade the pre-window to make it visually distinct
    ax.axvspan(-_PRE_WINDOW_MINUTES, 0, alpha=0.06, color=_TEXT, zorder=0)

    ax.set_xlabel("Minutes relative to session start", color=_TEXT, fontsize=9)
    ax.set_ylabel(f"Server messages per {_BIN_MINUTES} min", color=_TEXT, fontsize=9)
    ax.set_title(
        f"{user_display_name} — Session Burst Profile  ·  {n_sessions} session{'s' if n_sessions != 1 else ''}",
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


# ---------------------------------------------------------------------------
# Burst ranking — highest / lowest burst increase across all users
# ---------------------------------------------------------------------------

def query_burst_ranking(
    conn: sqlite3.Connection,
    guild_id: int,
    min_sessions: int = 3,
) -> list[tuple[int, float, float, int]]:
    """
    Compute the session burst increase for every user in the guild.

    For each user, "increase" = mean post-session msg rate minus mean pre-session
    msg rate (both in messages per _BIN_MINUTES-minute bin, averaged over all
    sessions and all bins in the window).

    Returns a list of (user_id, pre_avg, post_avg, n_sessions) sorted by
    (post_avg - pre_avg) descending.  Only users with at least *min_sessions*
    sessions are included.
    """
    rows = conn.execute(
        """
        SELECT user_id, created_at FROM processed_messages
        WHERE guild_id = ?
        ORDER BY user_id, created_at
        """,
        (guild_id,),
    ).fetchall()

    user_ts_map: dict[int, list[float]] = {}
    for uid, grp in groupby(rows, key=lambda r: r[0]):
        ts_list = [float(r[1]) for r in grp]
        if len(ts_list) >= 2:
            user_ts_map[int(uid)] = ts_list

    if not user_ts_map:
        return []

    guild_rows = conn.execute(
        "SELECT created_at FROM processed_messages WHERE guild_id = ? ORDER BY created_at",
        (guild_id,),
    ).fetchall()
    guild_ts = [float(r[0]) for r in guild_rows]

    bin_secs = _BIN_MINUTES * 60
    pre_secs = _PRE_WINDOW_MINUTES * 60
    post_secs = _POST_WINDOW_MINUTES * 60
    pre_bins_count = _PRE_WINDOW_MINUTES // _BIN_MINUTES
    post_bins_count = _POST_WINDOW_MINUTES // _BIN_MINUTES

    results: list[tuple[int, float, float, int]] = []

    for user_id, user_ts in user_ts_map.items():
        session_starts: list[float] = [user_ts[0]]
        for i in range(1, len(user_ts)):
            if user_ts[i] - user_ts[i - 1] >= _IDLE_THRESHOLD_SECONDS:
                session_starts.append(user_ts[i])

        if len(session_starts) < min_sessions:
            continue

        pre_sum = [0.0] * pre_bins_count
        post_sum = [0.0] * post_bins_count

        for start_ts in session_starts:
            lo = bisect.bisect_left(guild_ts, start_ts - pre_secs)
            hi = bisect.bisect_left(guild_ts, start_ts)
            for ts in guild_ts[lo:hi]:
                offset = ts - (start_ts - pre_secs)
                bin_i = int(offset // bin_secs)
                if 0 <= bin_i < pre_bins_count:
                    pre_sum[bin_i] += 1

            lo = bisect.bisect_left(guild_ts, start_ts)
            for ts in guild_ts[lo:]:
                offset = ts - start_ts
                if offset >= post_secs:
                    break
                bin_i = int(offset // bin_secs)
                if 0 <= bin_i < post_bins_count:
                    post_sum[bin_i] += 1

        n = len(session_starts)
        pre_avg = sum(pre_sum) / (pre_bins_count * n)
        post_avg = sum(post_sum) / (post_bins_count * n)
        results.append((user_id, pre_avg, post_avg, n))

    results.sort(key=lambda r: r[2] - r[1], reverse=True)
    return results


def render_burst_ranking_chart(
    entries: list[tuple[str, float, float, int]],
    limit: int,
    guild_name: str,
) -> bytes:
    """
    Render a horizontal bar chart showing the top and bottom *limit* users
    by burst increase (post_avg - pre_avg).

    entries: list of (display_name, pre_avg, post_avg, n_sessions)
             sorted highest-increase first.
    """
    if not entries:
        raise ValueError("No entries to render.")

    top = entries[:limit]
    bottom = entries[-limit:] if len(entries) > limit else []

    names: list[str] = []
    values: list[float] = []
    colors: list[str] = []
    n_sessions_list: list[int] = []

    for name, pre, post, n in reversed(top):
        names.append(name)
        values.append(post - pre)
        colors.append(_BAR)
        n_sessions_list.append(n)

    if bottom:
        for name, pre, post, n in reversed(bottom):
            names.append(name)
            values.append(post - pre)
            colors.append(_BAR_ACCENT)
            n_sessions_list.append(n)

    fig_height = max(4.5, len(names) * 0.45 + 1.5)
    fig, ax = plt.subplots(figsize=(9, fig_height))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    y = list(range(len(names)))
    bars = ax.barh(y, values, color=colors, height=0.7, zorder=2)

    val_range = (max(values) - min(values)) if len(values) > 1 else (abs(values[0]) if values else 1.0)
    nudge = val_range * 0.012 or 0.01

    for bar, n in zip(bars, n_sessions_list):
        width = bar.get_width()
        x_pos = width + nudge if width >= 0 else width - nudge
        ax.text(
            x_pos,
            bar.get_y() + bar.get_height() / 2,
            f"{n}s",
            va="center",
            ha="left" if width >= 0 else "right",
            color=_TEXT,
            fontsize=7,
        )

    ax.axvline(0, color=_GRID, linewidth=1, zorder=3)

    ax.set_yticks(y)
    ax.set_yticklabels(names, color=_TEXT, fontsize=9)
    ax.set_xlabel(f"Burst increase (msg / {_BIN_MINUTES} min, post - pre session avg)", color=_TEXT, fontsize=9)
    ax.set_title(
        f"{guild_name} - Session Burst Ranking",
        color=_TEXT,
        fontsize=13,
        pad=10,
    )
    ax.tick_params(axis="x", colors=_TEXT, labelsize=8, length=0)
    ax.tick_params(axis="y", length=0)
    ax.xaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)

    for spine in ax.spines.values():
        spine.set_visible(False)

    from matplotlib.patches import Patch
    legend_handles = [Patch(color=_BAR, label=f"Top {limit} highest burst")]
    if bottom:
        legend_handles.append(Patch(color=_BAR_ACCENT, label=f"Bottom {limit} lowest burst"))
    ax.legend(handles=legend_handles, facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT, fontsize=9)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Message cadence — inter-message time stats over time
# ---------------------------------------------------------------------------

@dataclass
class CadenceBucket:
    label: str
    min_gap: float     # wick low
    p20_gap: float     # body low (open)
    median_gap: float  # body mid
    p80_gap: float     # body high (close)
    max_gap: float     # wick high


def query_message_cadence(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    utc_offset_hours: float = 0,
    channel_id: int | None = None,
) -> list[CadenceBucket]:
    """Compute per-bucket inter-message time statistics.

    Returns a list of CadenceBucket with average, mode, and 80th-percentile
    gap durations (in minutes) between consecutive messages.
    """
    now = datetime.now(timezone.utc)
    offset_secs = int(utc_offset_hours * 3600)

    # hour_of_day / day_of_week: aggregate across all history into fixed bins
    if resolution in ("hour_of_day", "day_of_week"):
        params: list[object] = [guild_id]
        channel_clause = ""
        if channel_id is not None:
            channel_clause = " AND channel_id = ?"
            params.append(channel_id)

        rows = conn.execute(
            f"SELECT ts FROM messages WHERE guild_id = ?{channel_clause} ORDER BY ts",
            params,
        ).fetchall()
        if not rows:
            return []

        if resolution == "hour_of_day":
            labels_list, n_bins = _HOD_LABELS, 24
        else:
            labels_list, n_bins = _DOW_LABELS, 7

        gap_buckets: dict[int, list[float]] = {i: [] for i in range(n_bins)}
        prev_ts = int(rows[0]["ts"])
        for row in rows[1:]:
            cur_ts = int(row["ts"])
            gap_sec = cur_ts - prev_ts
            prev_ts = cur_ts
            if gap_sec <= 0:
                continue
            dt = datetime.fromtimestamp(cur_ts + offset_secs, tz=timezone.utc)
            if resolution == "hour_of_day":
                idx = dt.hour
            else:
                idx = (dt.weekday() + 1) % 7  # Mon=0 -> Sun=0,Mon=1,...,Sat=6
            gap_buckets[idx].append(gap_sec / 60.0)

        results: list[CadenceBucket] = []
        for i in range(n_bins):
            gaps = gap_buckets[i]
            if not gaps:
                results.append(CadenceBucket(label=labels_list[i], min_gap=0, p20_gap=0, median_gap=0, p80_gap=0, max_gap=0))
                continue
            sg = sorted(gaps)
            results.append(CadenceBucket(
                label=labels_list[i],
                min_gap=sg[0],
                p20_gap=sg[int(len(sg) * 0.2)],
                median_gap=float(statistics.median(sg)),
                p80_gap=sg[int(len(sg) * 0.8)],
                max_gap=sg[-1],
            ))
        return results

    # Time-series resolutions
    if resolution == "day":
        buckets, start_ts = _day_buckets(now, utc_offset_hours)
    elif resolution == "week":
        buckets, start_ts = _week_buckets(now, utc_offset_hours)
    elif resolution == "month":
        buckets, start_ts = _month_buckets(now, utc_offset_hours)
    elif resolution == "hour":
        buckets, start_ts = _hour_buckets(now, utc_offset_hours)
    else:
        buckets, start_ts = _day_buckets(now, utc_offset_hours)

    params2: list[object] = [guild_id, int(start_ts)]
    channel_clause2 = ""
    if channel_id is not None:
        channel_clause2 = " AND channel_id = ?"
        params2.append(channel_id)

    rows = conn.execute(
        f"SELECT ts FROM messages WHERE guild_id = ? AND ts >= ?{channel_clause2} "
        "ORDER BY ts",
        params2,
    ).fetchall()

    if not rows:
        return []

    timestamps = [int(r["ts"]) for r in rows]

    n_buckets = len(buckets)
    end_ts = datetime.now(timezone.utc).timestamp()
    span = end_ts - start_ts
    bucket_size = span / n_buckets
    bucket_boundaries = [start_ts + bucket_size * i for i in range(n_buckets)]

    ts_gap_buckets: dict[int, list[float]] = {i: [] for i in range(n_buckets)}
    for j in range(1, len(timestamps)):
        gap_sec = timestamps[j] - timestamps[j - 1]
        if gap_sec <= 0:
            continue
        idx = bisect.bisect_right(bucket_boundaries, timestamps[j]) - 1
        idx = max(0, min(idx, n_buckets - 1))
        ts_gap_buckets[idx].append(gap_sec / 60.0)

    results2: list[CadenceBucket] = []
    for i, (_key, label) in enumerate(buckets):
        gaps = ts_gap_buckets[i]
        if not gaps:
            results2.append(CadenceBucket(label=label, min_gap=0, p20_gap=0, median_gap=0, p80_gap=0, max_gap=0))
            continue

        sg = sorted(gaps)
        results2.append(CadenceBucket(
            label=label,
            min_gap=sg[0],
            p20_gap=sg[int(len(sg) * 0.2)],
            median_gap=float(statistics.median(sg)),
            p80_gap=sg[int(len(sg) * 0.8)],
            max_gap=sg[-1],
        ))

    return results2


def render_message_cadence_chart(
    buckets: list[CadenceBucket],
    title: str,
) -> bytes:
    """Render a candlestick chart of inter-message gap times.

    Each candle:
      wick  = min → max gap
      body  = p20 → p80 gap
      tick  = median
    Green body when median decreased vs prior bucket, pink when increased.
    """
    labels = [b.label for b in buckets]
    n = len(labels)
    fig_width = max(9, n * 0.5)
    fig, ax = plt.subplots(figsize=(fig_width, 4.5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    body_width = 0.5
    wick_color = "#72767d"
    color_down = "#57f287"   # green — median decreased (faster chat)
    color_up = "#eb459e"     # pink — median increased (slower chat)

    prev_median = None
    for i, b in enumerate(buckets):
        if b.max_gap == 0:
            prev_median = None
            continue

        # Wick: min to max
        ax.plot([i, i], [b.min_gap, b.max_gap], color=wick_color, linewidth=1, zorder=2)

        # Body color: green if median went down, pink if up
        if prev_median is not None and b.median_gap <= prev_median:
            color = color_down
        else:
            color = color_up

        # Body: p20 to p80
        body_bottom = b.p20_gap
        body_height = b.p80_gap - b.p20_gap
        ax.bar(i, body_height, bottom=body_bottom, width=body_width,
               color=color, edgecolor=color, linewidth=0.5, zorder=3)

        # Median tick
        ax.plot([i - body_width / 2, i + body_width / 2],
                [b.median_gap, b.median_gap],
                color=_TEXT, linewidth=1.5, zorder=4)

        prev_median = b.median_gap

    max_visible = 20
    x = list(range(n))
    if n > max_visible:
        step = max(1, n // max_visible)
        tick_positions = list(range(0, n, step))
        tick_labels_visible = [labels[i] for i in tick_positions]
    else:
        tick_positions = x
        tick_labels_visible = labels

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8)
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    ax.tick_params(length=0)

    ax.set_yscale("log")
    ax.yaxis.grid(True, color=_GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(title, color=_TEXT, fontsize=13, pad=10)
    ax.set_ylabel("Minutes between messages (log)", color=_TEXT, fontsize=9)

    for spine in ax.spines.values():
        spine.set_visible(False)

    from matplotlib.patches import Patch
    legend_handles = [
        Patch(color=color_down, label="Median \u2193 (faster)"),
        Patch(color=color_up, label="Median \u2191 (slower)"),
    ]
    ax.legend(handles=legend_handles, facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT, fontsize=9)

    plt.tight_layout(pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Member join histogram
# ---------------------------------------------------------------------------


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
    ax.set_xticklabels(tick_labels_visible, rotation=45, ha="right", color=_TEXT, fontsize=8)
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
