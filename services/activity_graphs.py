"""Activity graph generation — message counts bucketed by time resolution."""
from __future__ import annotations

import io
import sqlite3
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


def _hour_buckets(now: datetime) -> tuple[list[tuple[str, str]], float]:
    """24 hourly buckets ending at the current hour."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    buckets = []
    for i in range(24):
        dt = start + timedelta(hours=i)
        key = dt.strftime("%Y-%m-%d %H")
        label = dt.strftime("%a %H:%M")
        buckets.append((key, label))
    return buckets, start.timestamp()


def _day_buckets(now: datetime) -> tuple[list[tuple[str, str]], float]:
    """30 daily buckets ending today."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
    buckets = []
    for i in range(30):
        dt = start + timedelta(days=i)
        key = dt.strftime("%Y-%m-%d")
        label = dt.strftime("%b %d")
        buckets.append((key, label))
    return buckets, start.timestamp()


def _week_buckets(now: datetime) -> tuple[list[tuple[str, str]], float]:
    """12 weekly buckets (Monday-based) ending this week."""
    days_since_monday = now.weekday()
    this_monday = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = this_monday - timedelta(weeks=11)
    buckets = []
    for i in range(12):
        dt = start + timedelta(weeks=i)
        key = dt.strftime("%Y-%W")
        label = dt.strftime("%b %d")
        buckets.append((key, label))
    return buckets, start.timestamp()


def _month_buckets(now: datetime) -> tuple[list[tuple[str, str]], float]:
    """12 monthly buckets ending this month."""
    buckets = []
    for i in range(11, -1, -1):
        m = now.month - i
        y = now.year
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


def _strftime_expr(resolution: Resolution) -> str:
    """SQLite strftime expression that buckets created_at into the right key format."""
    if resolution == "hour":
        return "strftime('%Y-%m-%d %H', datetime(created_at, 'unixepoch'))"
    if resolution == "day":
        return "strftime('%Y-%m-%d', datetime(created_at, 'unixepoch'))"
    if resolution == "week":
        return "strftime('%Y-%W', datetime(created_at, 'unixepoch'))"
    return "strftime('%Y-%m', datetime(created_at, 'unixepoch'))"


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
) -> tuple[list[str], list[int], list[int]]:
    """
    Query message counts and unique active members per time bucket.

    Returns (labels, message_counts, unique_member_counts).
    Empty buckets are filled with 0.
    """
    now = datetime.now(timezone.utc)
    bucket_sequence, since_ts = _BUCKET_BUILDERS[resolution](now)
    bucket_expr = _strftime_expr(resolution)

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

    msg_by_key = {row[0]: int(row[1]) for row in rows}
    members_by_key = {row[0]: int(row[2]) for row in rows}

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
) -> tuple[list[str], list[int]]:
    """
    Aggregate message counts by hour-of-day (0-23) or day-of-week (0=Sun..6=Sat)
    across all recorded history.

    Returns (labels, message_counts).
    """
    if resolution == "hour_of_day":
        expr = "CAST(strftime('%H', datetime(created_at, 'unixepoch')) AS INTEGER)"
        labels = _HOD_LABELS
        n = 24
    else:
        expr = "CAST(strftime('%w', datetime(created_at, 'unixepoch')) AS INTEGER)"
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
    now = datetime.now(timezone.utc).timestamp()
    mid = now - period_seconds
    start = mid - period_seconds

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
# Chart rendering
# ---------------------------------------------------------------------------


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
