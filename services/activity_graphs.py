"""Activity graph generation — message counts bucketed by time resolution."""
from __future__ import annotations

import bisect
import io
import sqlite3
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
    bucket_expr = _strftime_expr(resolution)

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
