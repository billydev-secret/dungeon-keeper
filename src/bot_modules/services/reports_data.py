"""Shared, JSON-serializable data layer for reports.

Both the ``/report`` slash commands in ``reports.py`` and the web dashboard
routes in ``web/routes/reports.py`` call into this module so the two surfaces
stay in sync. Functions here are synchronous and do NOT touch ``discord.py``
objects — callers snapshot any required Discord state on the event loop before
dispatching to ``asyncio.to_thread``.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal, TypedDict

from bot_modules.core.bot_exclusion import bot_filter_clause
from bot_modules.services.channel_rollup import ChannelResolver, build_resolver
from bot_modules.services.activity_graphs import (
    MIN_BAND_PERIODS,
    OverlayPeriod,
    Resolution,
    query_activity_overlay,
    query_dropoff_profiles,
    query_message_activity,
    query_message_histogram,
    query_nsfw_gender_activity,
    query_xp_activity_with_breakdown,
    query_xp_histogram_with_breakdown,
    xp_histogram_window_label,
)

# ---------------------------------------------------------------------------
# Window label lookups
# ---------------------------------------------------------------------------

_WINDOW_LABELS: dict[str, str] = {
    "hour": "Last 24 Hours",
    "day": "Last 30 Days",
    "week": "Last 12 Weeks",
    "month": "Last 12 Months",
    "hour_of_day": "By Hour of Day",
    "day_of_week": "By Day of Week",
}

# The two overlay views are not timeline resolutions — they share the Activity
# route but bucket by position *inside* a period. Kept as a separate alias so
# ``Resolution`` keeps meaning "something _BUCKET_BUILDERS can build".
ActivityResolution = Resolution | Literal["day_overlay", "week_overlay"]

_OVERLAY_PERIODS: dict[str, OverlayPeriod] = {
    "day_overlay": "day",
    "week_overlay": "week",
}

_OVERLAY_NOUNS: dict[str, tuple[str, str]] = {
    # resolution -> (what the bold line is, the unit the band counts in)
    "day_overlay": ("Today", "Days"),
    "week_overlay": ("This Week", "Weeks"),
}

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

_GENDER_COLORS: dict[str, str] = {
    "male": "#5865f2",
    "female": "#eb459e",
    "nonbinary": "#57f287",
    "unknown": "#72767d",
}

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


# ---------------------------------------------------------------------------
# MemberSnapshot — thread-safe stand-in for discord.Member
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    user_id: int
    display_name: str
    is_bot: bool
    joined_at: float | None  # epoch seconds
    role_ids: tuple[int, ...]


# ---------------------------------------------------------------------------
# Role growth
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Message cadence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Join times
# ---------------------------------------------------------------------------


class JoinTimesData(TypedDict):
    resolution: str
    labels: list[str]
    counts: list[int]


def get_join_times_data(
    members: list[MemberSnapshot],
    resolution: Literal["hour_of_day", "day_of_week"],
    utc_offset_hours: float,
) -> JoinTimesData:
    if resolution == "hour_of_day":
        labels, n_bins = _HOD_LABELS, 24
    else:
        labels, n_bins = _DOW_LABELS, 7

    offset_secs = int(utc_offset_hours * 3600)
    counts = [0] * n_bins
    for m in members:
        if m.is_bot or m.joined_at is None:
            continue
        ts = m.joined_at + offset_secs
        local_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if resolution == "hour_of_day":
            counts[local_dt.hour] += 1
        else:
            counts[(local_dt.weekday() + 1) % 7] += 1

    return {
        "resolution": resolution,
        "labels": labels,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# NSFW gender activity
# ---------------------------------------------------------------------------


class GenderSeries(TypedDict):
    gender: str
    counts: list[int]
    color: str


class NsfwGenderData(TypedDict):
    resolution: str
    window_label: str
    media_only: bool
    labels: list[str]
    series: list[GenderSeries]


def get_nsfw_gender_data(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    channel_ids: list[int],
    utc_offset_hours: float,
    media_only: bool,
    include_bots: bool = False,
) -> NsfwGenderData:
    labels, gender_counts = query_nsfw_gender_activity(
        conn,
        guild_id,
        resolution,
        channel_ids,
        utc_offset_hours=utc_offset_hours,
        media_only=media_only,
        include_bots=include_bots,
    )
    series: list[GenderSeries] = [
        {"gender": g, "counts": c, "color": _GENDER_COLORS.get(g, "#72767d")}
        for g, c in gender_counts.items()
    ]
    return {
        "resolution": resolution,
        "window_label": _WINDOW_LABELS.get(resolution, resolution),
        "media_only": media_only,
        "labels": labels,
        "series": series,
    }


# ---------------------------------------------------------------------------
# Message rate (10-min buckets over 24h)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Greeter response times
# ---------------------------------------------------------------------------


class ResponseBucket(TypedDict):
    label: str
    count: int


class GreeterSession(TypedDict):
    user_id: int
    joined_at: float
    left_at: float | None


class GreeterResponseEntry(TypedDict):
    user_id: str
    joined_at: float
    status: str
    greeted_at: float | None
    response_seconds: float | None
    wait_seconds: float | None
    greeter_id: str
    left_at: float | None


class GreeterResponseData(TypedDict):
    window_label: str
    total_joins: int
    count: int
    left_before_greeting_count: int
    awaiting_greeting_count: int
    median_seconds: float
    mean_seconds: float
    histogram: list[ResponseBucket]
    response_times_seconds: list[float]
    entries: list[GreeterResponseEntry]


def get_greeter_log_sessions(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    since_ts: float = 0.0,
) -> list[GreeterSession]:
    rows = conn.execute(
        """
        SELECT user_id, event_type, ts FROM member_events
        WHERE guild_id = ? AND ts >= ?
        ORDER BY ts
        """,
        (guild_id, since_ts),
    ).fetchall()

    joins_by_user: dict[int, list[float]] = defaultdict(list)
    leaves_by_user: dict[int, list[float]] = defaultdict(list)

    for row in rows:
        user_id = int(row["user_id"])
        ts = float(row["ts"])
        if row["event_type"] == "join":
            joins_by_user[user_id].append(ts)
        elif row["event_type"] == "leave":
            leaves_by_user[user_id].append(ts)

    sessions: list[GreeterSession] = []
    for user_id, join_times in joins_by_user.items():
        joins = sorted(join_times)
        leaves = sorted(leaves_by_user.get(user_id, []))
        leave_idx = 0

        for idx, joined_at in enumerate(joins):
            while leave_idx < len(leaves) and leaves[leave_idx] <= joined_at:
                leave_idx += 1

            next_join = joins[idx + 1] if idx + 1 < len(joins) else None
            left_at: float | None = None
            if leave_idx < len(leaves):
                candidate = leaves[leave_idx]
                if next_join is None or candidate < next_join:
                    left_at = candidate
                    leave_idx += 1

            sessions.append(
                {
                    "user_id": user_id,
                    "joined_at": joined_at,
                    "left_at": left_at,
                }
            )

    sessions.sort(key=lambda session: session["joined_at"])
    return sessions


def _query_greeter_response_details(
    conn: sqlite3.Connection,
    guild_id: int,
    greeter_channel_id: int,
    greeter_ids: set[int],
    sessions: list[GreeterSession],
    *,
    now_ts: float,
    include_bots: bool = False,
) -> list[GreeterResponseEntry]:
    if not sessions:
        return []

    # With no configured greeter list this falls back to "everyone who posted in
    # the greeter channel", which would otherwise let a welcome bot count as the
    # greeter and report a ~0s response time.
    bot_clause, bot_params = bot_filter_clause(guild_id, include_bots=include_bots)

    if greeter_ids:
        placeholders = ",".join("?" * len(greeter_ids))
        greeter_msgs = conn.execute(
            f"""
            SELECT author_id, ts FROM messages
            WHERE guild_id = ? AND channel_id = ?
              AND author_id IN ({placeholders})
            ORDER BY ts
            """,
            (guild_id, greeter_channel_id, *greeter_ids),
        ).fetchall()
    else:
        greeter_msgs = conn.execute(
            f"""
            SELECT author_id, ts FROM messages
            WHERE guild_id = ? AND channel_id = ?{bot_clause}
            ORDER BY ts
            """,
            (guild_id, greeter_channel_id, *bot_params),
        ).fetchall()

    greeter_times = [(int(r[0]), float(r[1])) for r in greeter_msgs]

    entries: list[GreeterResponseEntry] = []
    msg_idx = 0

    for session in sessions:
        user_id = session["user_id"]
        joined_at = session["joined_at"]
        left_at = session["left_at"]

        while msg_idx < len(greeter_times) and greeter_times[msg_idx][1] < joined_at:
            msg_idx += 1

        matched = False
        scan = msg_idx
        while scan < len(greeter_times):
            author_id, msg_ts = greeter_times[scan]
            if left_at is not None and msg_ts >= left_at:
                break
            if author_id != user_id:
                delta = max(0, msg_ts - joined_at)
                entries.append(
                    {
                        "user_id": str(user_id),
                        "joined_at": joined_at,
                        "status": "greeted",
                        "greeted_at": msg_ts,
                        "response_seconds": delta,
                        "wait_seconds": delta,
                        "greeter_id": str(author_id),
                        "left_at": left_at,
                    }
                )
                msg_idx = scan + 1
                matched = True
                break
            scan += 1

        if matched:
            continue

        if left_at is not None:
            entries.append(
                {
                    "user_id": str(user_id),
                    "joined_at": joined_at,
                    "status": "left_before_greeting",
                    "greeted_at": None,
                    "response_seconds": None,
                    "wait_seconds": max(0, left_at - joined_at),
                    "greeter_id": "",
                    "left_at": left_at,
                }
            )
        else:
            entries.append(
                {
                    "user_id": str(user_id),
                    "joined_at": joined_at,
                    "status": "awaiting_greeting",
                    "greeted_at": None,
                    "response_seconds": None,
                    "wait_seconds": max(0, now_ts - joined_at),
                    "greeter_id": "",
                    "left_at": None,
                }
            )

    return entries


def get_greeter_response_data(
    conn: sqlite3.Connection,
    guild_id: int,
    greeter_channel_id: int,
    greeter_ids: set[int],
    sessions: list[GreeterSession],
    *,
    now_ts: float | None = None,
    include_bots: bool = False,
) -> GreeterResponseData:
    now_ts = now_ts or datetime.now(timezone.utc).timestamp()
    entries = _query_greeter_response_details(
        conn,
        guild_id,
        greeter_channel_id,
        greeter_ids,
        sessions,
        now_ts=now_ts,
        include_bots=include_bots,
    )
    response_times = [
        float(entry["response_seconds"])
        for entry in entries
        if entry["response_seconds"] is not None
    ]

    bucket_counts = [0] * len(_RESPONSE_BUCKETS)
    for t in response_times:
        for i, (threshold, _) in enumerate(_RESPONSE_BUCKETS):
            if t < threshold:
                bucket_counts[i] += 1
                break

    med = statistics.median(response_times) if response_times else 0.0
    avg = statistics.mean(response_times) if response_times else 0.0

    entries.sort(key=lambda entry: entry["joined_at"], reverse=True)

    return {
        "window_label": "All Time",
        "total_joins": len(sessions),
        "count": len(response_times),
        "left_before_greeting_count": sum(
            1 for entry in entries if entry["status"] == "left_before_greeting"
        ),
        "awaiting_greeting_count": sum(
            1 for entry in entries if entry["status"] == "awaiting_greeting"
        ),
        "median_seconds": med,
        "mean_seconds": avg,
        "histogram": [
            {"label": label, "count": c}
            for (_, label), c in zip(_RESPONSE_BUCKETS, bucket_counts)
        ],
        "response_times_seconds": sorted(response_times),
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Activity (messages / XP)
# ---------------------------------------------------------------------------


class ActivitySeries(TypedDict):
    source: str
    counts: list[float]


class ActivityData(TypedDict):
    resolution: str
    window_label: str
    mode: str
    labels: list[str]
    # ``None`` only on the overlay views, where the current period has not been
    # lived through yet past the hour we are in. A covariant Sequence so the
    # timeline branches can still hand over a plain list[float].
    counts: Sequence[float | None]
    member_counts: list[int]
    show_members: bool
    y_label: str
    tz_label: str
    x_label: str
    series: list[ActivitySeries]
    # Overlay views only; empty elsewhere, and empty on an overlay whose sample
    # was too thin to summarise (see MIN_BAND_PERIODS).
    band_low: list[float]
    band_mid: list[float]
    band_high: list[float]
    periods_sampled: int


def get_activity_data(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: ActivityResolution,
    utc_offset_hours: float,
    mode: Literal["messages", "xp"] = "messages",
    user_id: int | None = None,
    channel_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    exclude_channel_ids: set[int] | None = None,
    compare_periods: int = 12,
) -> ActivityData:
    tz_label = f"UTC{utc_offset_hours:+g}" if utc_offset_hours else "UTC"
    show_members = user_id is None

    if resolution in ("day_overlay", "week_overlay"):
        return _get_overlay_data(
            conn,
            guild_id,
            resolution,
            utc_offset_hours,
            tz_label,
            mode=mode,
            compare_periods=compare_periods,
            user_id=user_id,
            channel_id=channel_id,
            exclude_user_ids=exclude_user_ids,
            exclude_channel_ids=exclude_channel_ids,
        )

    series: list[ActivitySeries] = []
    if mode == "xp":
        if resolution in ("hour_of_day", "day_of_week"):
            labels, xp_totals, by_source = query_xp_histogram_with_breakdown(
                conn,
                guild_id,
                resolution,  # type: ignore[arg-type]
                user_id=user_id,
                channel_id=channel_id,
                exclude_user_ids=exclude_user_ids,
                exclude_channel_ids=exclude_channel_ids,
                utc_offset_hours=utc_offset_hours,
            )
            counts: list[float] = xp_totals
            member_counts: list[int] = []
            show_members = False
        else:
            (
                labels,
                xp_totals,
                member_counts,
                by_source,
            ) = query_xp_activity_with_breakdown(
                conn,
                guild_id,
                resolution,
                user_id=user_id,
                channel_id=channel_id,
                exclude_user_ids=exclude_user_ids,
                exclude_channel_ids=exclude_channel_ids,
                utc_offset_hours=utc_offset_hours,
            )
            counts = xp_totals
        series = [
            {"source": src, "counts": vals}
            for src, vals in sorted(
                by_source.items(), key=lambda kv: -sum(kv[1])
            )
        ]
        y_label = "XP Earned"
    else:
        if resolution in ("hour_of_day", "day_of_week"):
            labels, msg_counts = query_message_histogram(
                conn,
                guild_id,
                resolution,  # type: ignore[arg-type]
                user_id=user_id,
                channel_id=channel_id,
                exclude_user_ids=exclude_user_ids,
                exclude_channel_ids=exclude_channel_ids,
                utc_offset_hours=utc_offset_hours,
            )
            counts = [float(c) for c in msg_counts]
            member_counts = []
            show_members = False
        else:
            labels, msg_counts, member_counts = query_message_activity(
                conn,
                guild_id,
                resolution,
                user_id=user_id,
                channel_id=channel_id,
                exclude_user_ids=exclude_user_ids,
                exclude_channel_ids=exclude_channel_ids,
                utc_offset_hours=utc_offset_hours,
            )
            counts = [float(c) for c in msg_counts]
        y_label = "Messages"

    # The XP histograms are windowed rather than all-time (the daily rollup
    # cannot answer hour-of-day), so the chart title has to say so — the
    # message histograms next to them still read the full archive.
    window_label = _WINDOW_LABELS.get(resolution, resolution)
    if mode == "xp" and resolution in ("hour_of_day", "day_of_week"):
        window_label = xp_histogram_window_label(window_label)

    result: ActivityData = {
        "resolution": resolution,
        "window_label": window_label,
        "mode": mode,
        "labels": labels,
        "counts": counts,
        "member_counts": member_counts,
        "show_members": show_members,
        "y_label": y_label,
        "tz_label": tz_label,
        "x_label": "Period",
        "series": series,
        "band_low": [],
        "band_mid": [],
        "band_high": [],
        "periods_sampled": 0,
    }
    return result


def _get_overlay_data(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: str,
    utc_offset_hours: float,
    tz_label: str,
    *,
    mode: Literal["messages", "xp"],
    compare_periods: int,
    user_id: int | None,
    channel_id: int | None,
    exclude_user_ids: set[int] | None,
    exclude_channel_ids: set[int] | None,
) -> ActivityData:
    """The current day/week drawn against a band over the last N of them.

    Deliberately returns no ``series`` and no ``member_counts``: the series
    axis is now "now versus history", so an XP source breakdown would need a
    third dimension, and a per-hour distinct-member count over a partial period
    against a band of medians is not a number worth drawing.
    """
    period = _OVERLAY_PERIODS[resolution]
    subject, unit = _OVERLAY_NOUNS[resolution]

    result_ov = query_activity_overlay(
        conn,
        guild_id,
        period,
        mode=mode,
        compare_periods=compare_periods,
        user_id=user_id,
        channel_id=channel_id,
        exclude_user_ids=exclude_user_ids,
        exclude_channel_ids=exclude_channel_ids,
        utc_offset_hours=utc_offset_hours,
    )

    if result_ov.has_band:
        window_label = f"{subject} vs Last {result_ov.periods_sampled} {unit}"
    elif result_ov.periods_sampled:
        # Some history, but too little to summarise honestly. Parenthetical
        # rather than a dash: the caption already joins on one.
        window_label = (
            f"{subject} (needs {MIN_BAND_PERIODS} past {unit.lower()} to "
            f"compare, has {result_ov.periods_sampled})"
        )
    else:
        window_label = f"{subject} (no past {unit.lower()} to compare against yet)"

    if result_ov.clamped and result_ov.periods_requested > result_ov.periods_sampled:
        window_label = xp_histogram_window_label(window_label)

    return {
        "resolution": resolution,
        "window_label": window_label,
        "mode": mode,
        "labels": result_ov.labels,
        "counts": result_ov.current,
        "member_counts": [],
        "show_members": False,
        "y_label": "XP Earned" if mode == "xp" else "Messages",
        "tz_label": tz_label,
        "x_label": "Hour of day" if period == "day" else "Hour of week",
        "series": [],
        "band_low": result_ov.band_low,
        "band_mid": result_ov.band_mid,
        "band_high": result_ov.band_high,
        "periods_sampled": result_ov.periods_sampled,
    }


# ---------------------------------------------------------------------------
# Invite effectiveness
# ---------------------------------------------------------------------------


class InviteeRow(TypedDict):
    invitee_id: str
    invitee_name: str
    active: bool


class InviterRow(TypedDict):
    inviter_id: str
    inviter_name: str
    invite_count: int
    still_active: int
    retention_pct: float
    invitees: list[InviteeRow]


class InviteEffectivenessData(TypedDict):
    total_invites: int
    total_active: int
    overall_retention_pct: float
    inviters: list[InviterRow]


def get_invite_effectiveness_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int | None = None,
    active_days: int = 30,
) -> InviteEffectivenessData:
    now = int(datetime.now(timezone.utc).timestamp())
    active_cutoff = now - active_days * 86400

    cutoff_clause = ""
    params: list[object] = [guild_id]
    if days is not None:
        cutoff_clause = "AND joined_at >= ?"
        params.append(now - days * 86400)

    rows = conn.execute(
        f"""
        SELECT inviter_id, invitee_id, joined_at
        FROM invite_edges
        WHERE guild_id = ? {cutoff_clause}
        ORDER BY inviter_id
        """,
        params,
    ).fetchall()

    if not rows:
        return {
            "total_invites": 0,
            "total_active": 0,
            "overall_retention_pct": 0.0,
            "inviters": [],
        }

    # Check which invitees are still active
    invitee_ids = [int(r[1]) for r in rows]
    ph = ",".join("?" * len(invitee_ids))
    active_rows = conn.execute(
        f"""
        SELECT user_id FROM member_activity
        WHERE guild_id = ? AND last_message_at >= ?
        AND user_id IN ({ph})
        """,
        [guild_id, active_cutoff, *invitee_ids],
    ).fetchall()
    active_set = {int(r[0]) for r in active_rows}

    inviter_data: dict[int, list[int]] = {}
    for r in rows:
        inviter_data.setdefault(int(r[0]), []).append(int(r[1]))

    inviters: list[InviterRow] = []
    total_invites = 0
    total_active = 0
    for inviter_id, invitees in inviter_data.items():
        count = len(invitees)
        active = sum(1 for i in invitees if i in active_set)
        total_invites += count
        total_active += active
        inviters.append(
            {
                "inviter_id": str(inviter_id),
                "inviter_name": "",
                "invite_count": count,
                "still_active": active,
                "retention_pct": round(active / count * 100, 1) if count else 0.0,
                "invitees": [
                    {"invitee_id": str(iid), "invitee_name": "", "active": iid in active_set}
                    for iid in invitees
                ],
            }
        )

    inviters.sort(key=lambda r: r["invite_count"], reverse=True)

    return {
        "total_invites": total_invites,
        "total_active": total_active,
        "overall_retention_pct": round(total_active / total_invites * 100, 1)
        if total_invites
        else 0.0,
        "inviters": inviters,
    }


# ---------------------------------------------------------------------------
# Interaction graph (social network)
# ---------------------------------------------------------------------------


class InteractionEdge(TypedDict):
    from_id: str
    from_name: str
    to_id: str
    to_name: str
    weight: int


class InteractionNode(TypedDict):
    user_id: str
    user_name: str
    total_outbound: int
    total_inbound: int
    unique_partners: int
    cluster_id: int


class InteractionGraphData(TypedDict, total=False):
    nodes: list[InteractionNode]
    edges: list[InteractionEdge]
    top_pairs: list[InteractionEdge]
    metrics: dict


# Bots are noise for the interaction graph — a member replying to a bot is not a
# real relationship. Allowlisted bots live in known_users with is_bot=1; drop any
# edge touching one. Each use consumes two guild_id params (mirrors the same
# fragment in interaction_graph.query_connection_web).
_EXCLUDE_BOT_ENDPOINTS = (
    "from_user_id NOT IN "
    "(SELECT user_id FROM known_users WHERE guild_id = ? AND is_bot = 1) "
    "AND to_user_id NOT IN "
    "(SELECT user_id FROM known_users WHERE guild_id = ? AND is_bot = 1)"
)


def get_interaction_graph_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int | None = None,
    limit: int = 50,
    include_metrics: bool = False,
    clustering_resolution: float = 1.2,
) -> InteractionGraphData:
    now = int(datetime.now(timezone.utc).timestamp())

    if days is not None:
        cutoff = now - days * 86400
        rows = conn.execute(
            f"""
            SELECT from_user_id, to_user_id, COUNT(*) as weight
            FROM user_interactions_log
            WHERE guild_id = ? AND ts >= ?
              AND {_EXCLUDE_BOT_ENDPOINTS}
            GROUP BY from_user_id, to_user_id
            ORDER BY weight DESC
            """,
            (guild_id, cutoff, guild_id, guild_id),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT from_user_id, to_user_id, weight
            FROM user_interactions
            WHERE guild_id = ?
              AND {_EXCLUDE_BOT_ENDPOINTS}
            ORDER BY weight DESC
            """,
            (guild_id, guild_id, guild_id),
        ).fetchall()

    edges: list[InteractionEdge] = []
    node_out: dict[int, int] = {}
    node_in: dict[int, int] = {}
    node_partners: dict[int, set[int]] = {}

    for r in rows:
        from_id, to_id, weight = int(r[0]), int(r[1]), int(r[2])
        if from_id == to_id:
            continue
        node_out[from_id] = node_out.get(from_id, 0) + weight
        node_in[to_id] = node_in.get(to_id, 0) + weight
        node_partners.setdefault(from_id, set()).add(to_id)
        node_partners.setdefault(to_id, set()).add(from_id)
        edges.append(
            {
                "from_id": str(from_id),
                "from_name": "",
                "to_id": str(to_id),
                "to_name": "",
                "weight": weight,
            }
        )

    # Top pairs: merge bidirectional
    pair_weights: dict[tuple[int, int], int] = {}
    for r in rows:
        a, b, w = int(r[0]), int(r[1]), int(r[2])
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        pair_weights[key] = pair_weights.get(key, 0) + w
    top_pairs: list[InteractionEdge] = []
    for (a, b), w in sorted(pair_weights.items(), key=lambda x: x[1], reverse=True)[
        :limit
    ]:
        top_pairs.append(
            {
                "from_id": str(a),
                "from_name": "",
                "to_id": str(b),
                "to_name": "",
                "weight": w,
            }
        )

    metrics: dict | None = None
    cluster_lookup: dict[str, int] = {}
    if include_metrics:
        from bot_modules.services.graph_metrics import compute_graph_metrics

        metrics = compute_graph_metrics(
            ((int(e["from_id"]), int(e["to_id"]), e["weight"]) for e in edges),
            top_n=limit,
            clustering_resolution=clustering_resolution,
        )
        cluster_lookup = metrics.get("node_cluster", {})
        # Strip the raw per-node cluster map from the response payload — the
        # frontend only needs cluster_id baked onto each node.
        metrics = {k: v for k, v in metrics.items() if k not in {"node_cluster", "graph_nodes", "graph_edges"}}

    all_ids = set(node_out.keys()) | set(node_in.keys())
    nodes: list[InteractionNode] = []
    for uid in sorted(
        all_ids, key=lambda u: node_out.get(u, 0) + node_in.get(u, 0), reverse=True
    )[:limit]:
        nodes.append(
            {
                "user_id": str(uid),
                "user_name": "",
                "total_outbound": node_out.get(uid, 0),
                "total_inbound": node_in.get(uid, 0),
                "unique_partners": len(node_partners.get(uid, set())),
                "cluster_id": cluster_lookup.get(str(uid), 0),
            }
        )

    result: InteractionGraphData = {
        "nodes": nodes,
        "edges": edges[: limit * 2],
        "top_pairs": top_pairs,
    }
    if metrics is not None:
        result["metrics"] = metrics
    return result


class InteractionSeriesNode(TypedDict):
    user_id: str
    user_name: str
    cluster_id: int
    joins: list[int]
    leaves: list[int]


class InteractionSeriesPair(TypedDict):
    a: str
    b: str
    w: list[int]


class InteractionSeriesData(TypedDict):
    bin_seconds: int
    start: int
    weeks: int
    nodes: list[InteractionSeriesNode]
    pairs: list[InteractionSeriesPair]


def get_interaction_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    weeks: int = 30,
    limit: int = 60,
    clustering_resolution: float = 1.2,
) -> InteractionSeriesData:
    """Weekly-binned interaction history for the Connection Graph's replay.

    One aggregation over ``user_interactions_log`` (the log spans months and
    the whole pair-week matrix computes in well under a second on prod data),
    returned as per-pair weight vectors the client composes into any rolling
    window. Pairs are undirected — replay is about who talks with whom, not
    direction. The roster is the top ``limit`` members by total interactions
    across the span, so a member who was central in week 2 and gone by week
    20 still appears; ``member_events`` join/leave stamps ride along so the
    client can pop a node in at arrival and drop it at departure instead of
    letting a trailing window keep ghosts around.

    ``cluster_id`` comes from ONE clustering pass (weighted label
    propagation, same as the live graph) over the whole span's summed weights:
    replay colours must stay stable while the frames move, and a
    current-window partition would paint every since-departed member as
    unknown. Bot endpoints are excluded exactly as in the live graph query.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    weeks = max(4, min(int(weeks), 60))
    limit = max(5, min(int(limit), 100))
    bin_seconds = 7 * 86400
    start = now - weeks * bin_seconds

    # The roster shortlist is chosen in SQL, before the pair-week matrix is
    # materialized: the unfiltered GROUP BY returns every (pair, week) row in
    # the guild — ~25k on the main guild today, and growing with the square of
    # the member count — to keep `limit` of them. Ranking users first and
    # joining against that set bounds what crosses into Python.
    rows = conn.execute(
        f"""
        WITH live AS (
            SELECT from_user_id, to_user_id, ts
            FROM user_interactions_log
            WHERE guild_id = ? AND ts >= ? AND from_user_id != to_user_id
              AND {_EXCLUDE_BOT_ENDPOINTS}
        ),
        totals AS (
            SELECT uid, SUM(n) AS total FROM (
                SELECT from_user_id AS uid, COUNT(*) AS n FROM live GROUP BY 1
                UNION ALL
                SELECT to_user_id AS uid, COUNT(*) AS n FROM live GROUP BY 1
            ) GROUP BY uid
            ORDER BY total DESC
            LIMIT ?
        )
        SELECT MIN(from_user_id, to_user_id) AS a,
               MAX(from_user_id, to_user_id) AS b,
               CAST((ts - ?) / ? AS INTEGER) AS bin,
               COUNT(*) AS w
        FROM live
        WHERE from_user_id IN (SELECT uid FROM totals)
          AND to_user_id   IN (SELECT uid FROM totals)
        GROUP BY a, b, bin
        """,
        (guild_id, start, guild_id, guild_id, limit, start, bin_seconds),
    ).fetchall()

    pair_bins: dict[tuple[int, int], list[int]] = {}
    node_total: dict[int, int] = {}
    for r in rows:
        a, b, bin_i, w = int(r[0]), int(r[1]), int(r[2]), int(r[3])
        # ts == now lands in bin `weeks`; fold it into the last bin.
        bin_i = min(bin_i, weeks - 1)
        vec = pair_bins.setdefault((a, b), [0] * weeks)
        vec[bin_i] += w
        node_total[a] = node_total.get(a, 0) + w
        node_total[b] = node_total.get(b, 0) + w

    # Both endpoints are already inside the SQL shortlist; only the pair floor
    # is left to apply.
    kept = {pair: vec for pair, vec in pair_bins.items() if sum(vec) >= 2}

    # The roster is who SURVIVES the floor, not who made the shortlist: a
    # member whose every pair is a one-off drops out entirely. Otherwise they
    # ship as a node the client can never draw (no pair, so never present in
    # any window) while defaulting to cluster 0 — which is the LARGEST real
    # community, not an "unclustered" sentinel — inflating that community's
    # chip count with members who appear in no frame.
    roster = {uid for pair in kept for uid in pair}

    # Stable replay colours: one partition over the whole span.
    from bot_modules.services.graph_metrics import compute_graph_metrics

    metrics = compute_graph_metrics(
        ((a, b, sum(vec)) for (a, b), vec in kept.items()),
        top_n=limit,
        clustering_resolution=clustering_resolution,
    )
    cluster_lookup: dict[str, int] = metrics.get("node_cluster", {})

    ev_rows = conn.execute(
        "SELECT user_id, event_type, ts FROM member_events"
        " WHERE guild_id = ? AND ts >= ?",
        (guild_id, start),
    ).fetchall()
    joins: dict[int, list[int]] = {}
    leaves: dict[int, list[int]] = {}
    for r in ev_rows:
        uid, etype, ts = int(r[0]), str(r[1]), int(r[2])
        if uid not in roster:
            continue
        if etype == "join":
            joins.setdefault(uid, []).append(ts)
        elif etype == "leave":
            leaves.setdefault(uid, []).append(ts)

    nodes: list[InteractionSeriesNode] = [
        {
            "user_id": str(uid),
            "user_name": "",
            "cluster_id": cluster_lookup.get(str(uid), 0),
            "joins": sorted(joins.get(uid, [])),
            "leaves": sorted(leaves.get(uid, [])),
        }
        for uid in sorted(roster, key=lambda u: node_total[u], reverse=True)
    ]
    pairs: list[InteractionSeriesPair] = [
        {"a": str(a), "b": str(b), "w": vec} for (a, b), vec in kept.items()
    ]
    return {
        "bin_seconds": bin_seconds,
        "start": start,
        "weeks": weeks,
        "nodes": nodes,
        "pairs": pairs,
    }


# ---------------------------------------------------------------------------
# Member retention / dropoff
# ---------------------------------------------------------------------------


class RetentionEntry(TypedDict):
    user_id: str
    user_name: str
    msgs_prev: int
    msgs_recent: int
    drop_pct: float
    normalized_drop_pct: float
    days_active_prev: int
    days_active_recent: int
    last_seen_ts: float | None
    level: int
    total_xp: float


class RetentionData(TypedDict):
    period_days: int
    total_dropoffs: int
    server_activity_change_pct: float
    entries: list[RetentionEntry]


def get_retention_data(
    conn: sqlite3.Connection,
    guild_id: int,
    period_days: int = 30,
    min_previous: int = 5,
    limit: int = 25,
    include_bots: bool = False,
) -> RetentionData:
    period_seconds = period_days * 86400
    profiles = query_dropoff_profiles(
        conn,
        guild_id,
        period_seconds,
        min_previous=min_previous,
        limit=limit,
        include_bots=include_bots,
    )

    # Server-wide activity ratio for normalization (from first profile)
    srv_prev = profiles[0].server_msgs_prev if profiles else 0
    srv_recent = profiles[0].server_msgs_recent if profiles else 0
    server_ratio = srv_recent / max(srv_prev, 1)

    entries: list[RetentionEntry] = []
    for p in profiles:
        drop_pct = round((1 - p.msgs_recent / max(p.msgs_prev, 1)) * 100, 1)
        # Normalized: adjust member's ratio by server-wide trend
        member_ratio = p.msgs_recent / max(p.msgs_prev, 1)
        if server_ratio > 0:
            adjusted_ratio = member_ratio / server_ratio
        else:
            adjusted_ratio = member_ratio
        normalized_drop_pct = round(max(0, (1 - adjusted_ratio)) * 100, 1)
        entries.append(
            {
                "user_id": str(p.user_id),
                "user_name": "",
                "msgs_prev": p.msgs_prev,
                "msgs_recent": p.msgs_recent,
                "drop_pct": drop_pct,
                "normalized_drop_pct": normalized_drop_pct,
                "days_active_prev": p.days_prev,
                "days_active_recent": p.days_recent,
                "last_seen_ts": p.last_seen_ts,
                "level": p.level,
                "total_xp": p.total_xp,
            }
        )

    server_change_pct = round((server_ratio - 1) * 100, 1)

    return {
        "period_days": period_days,
        "total_dropoffs": len(entries),
        "server_activity_change_pct": server_change_pct,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Voice activity
# ---------------------------------------------------------------------------


class VoiceUserRow(TypedDict):
    user_id: str
    user_name: str
    total_minutes: float
    session_count: int
    avg_minutes: float


class VoiceHourBucket(TypedDict):
    hour: int
    label: str
    total_minutes: float


class VoiceActivityData(TypedDict):
    total_sessions: int
    total_minutes: float
    avg_session_minutes: float
    top_users: list[VoiceUserRow]
    by_hour: list[VoiceHourBucket]


def get_voice_activity_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int | None = None,
    utc_offset_hours: float = 0,
) -> VoiceActivityData:
    """Derive voice activity from xp_events (source='voice').

    Each voice XP event represents one interval (≈1 minute) of qualified voice
    time.  Sessions are estimated by grouping consecutive events per user with
    a gap threshold of 5 minutes.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    SESSION_GAP = 300  # 5 minutes — new session if gap exceeds this

    cutoff_clause = ""
    params: list[object] = [guild_id]
    if days is not None:
        cutoff_clause = "AND created_at >= ?"
        params.append(now - days * 86400)

    # Top users by total voice minutes (each event = 1 minute)
    user_rows = conn.execute(
        f"""
        SELECT user_id, COUNT(*) AS total_minutes
        FROM xp_events
        WHERE guild_id = ? AND source = 'voice' {cutoff_clause}
        GROUP BY user_id
        ORDER BY total_minutes DESC
        LIMIT 30
        """,
        params,
    ).fetchall()

    # For session counts, fetch per-user timestamps for the top users
    top_user_ids = [int(r[0]) for r in user_rows]
    session_counts: dict[int, int] = {}
    if top_user_ids:
        ph = ",".join("?" * len(top_user_ids))
        ts_rows = conn.execute(
            f"""
            SELECT user_id, created_at
            FROM xp_events
            WHERE guild_id = ? AND source = 'voice' {cutoff_clause}
              AND user_id IN ({ph})
            ORDER BY user_id, created_at
            """,
            [*params, *top_user_ids],
        ).fetchall()

        prev_uid, prev_ts, sessions = None, 0.0, 0
        for uid, ts in ts_rows:
            uid = int(uid)
            if uid != prev_uid:
                if prev_uid is not None:
                    session_counts[prev_uid] = sessions
                prev_uid, sessions = uid, 1
            elif ts - prev_ts > SESSION_GAP:
                sessions += 1
            prev_ts = float(ts)
        if prev_uid is not None:
            session_counts[prev_uid] = sessions

    top_users: list[VoiceUserRow] = []
    for r in user_rows:
        uid = int(r[0])
        minutes = int(r[1])
        sessions = session_counts.get(uid, 1)
        top_users.append(
            {
                "user_id": str(uid),
                "user_name": "",
                "total_minutes": float(minutes),
                "session_count": sessions,
                "avg_minutes": round(minutes / max(sessions, 1), 1),
            }
        )

    # By hour of day
    hour_rows = conn.execute(
        f"""
        SELECT CAST(strftime('%H', datetime(created_at + {int(utc_offset_hours * 3600)}, 'unixepoch')) AS INTEGER) AS hr,
            COUNT(*) AS minutes
        FROM xp_events
        WHERE guild_id = ? AND source = 'voice' {cutoff_clause}
        GROUP BY hr
        ORDER BY hr
        """,
        params,
    ).fetchall()

    by_hour: list[VoiceHourBucket] = []
    hour_map = {int(r[0]): int(r[1]) for r in hour_rows}
    for h in range(24):
        by_hour.append(
            {
                "hour": h,
                "label": _HOD_LABELS[h],
                "total_minutes": float(hour_map.get(h, 0)),
            }
        )

    # Totals
    total_row = conn.execute(
        f"""
        SELECT COUNT(*) FROM xp_events
        WHERE guild_id = ? AND source = 'voice' {cutoff_clause}
        """,
        params,
    ).fetchone()
    total_minutes = float(total_row[0]) if total_row else 0.0

    # Estimate total sessions from all users
    all_ts_rows = conn.execute(
        f"""
        SELECT user_id, created_at FROM xp_events
        WHERE guild_id = ? AND source = 'voice' {cutoff_clause}
        ORDER BY user_id, created_at
        """,
        params,
    ).fetchall()
    total_sessions = 0
    prev_uid, prev_ts = None, 0.0
    for uid, ts in all_ts_rows:
        uid = int(uid)
        if uid != prev_uid:
            total_sessions += 1
            prev_uid = uid
        elif ts - prev_ts > SESSION_GAP:
            total_sessions += 1
        prev_ts = float(ts)

    return {
        "total_sessions": total_sessions,
        "total_minutes": total_minutes,
        "avg_session_minutes": round(total_minutes / max(total_sessions, 1), 1),
        "top_users": top_users,
        "by_hour": by_hour,
    }


# ---------------------------------------------------------------------------
# XP leaderboard / distribution
# ---------------------------------------------------------------------------


class XpUserRow(TypedDict):
    user_id: str
    user_name: str
    level: int
    total_xp: float
    text_xp: float
    voice_xp: float
    reply_xp: float
    react_xp: float


class XpLevelBucket(TypedDict):
    level: int
    count: int


class XpLeaderboardData(TypedDict):
    total_users: int
    leaderboard: list[XpUserRow]
    level_distribution: list[XpLevelBucket]
    source_totals: dict[str, float]


def get_xp_leaderboard_data(
    conn: sqlite3.Connection,
    guild_id: int,
    limit: int = 30,
    days: int | None = None,
) -> XpLeaderboardData:
    import time as _time

    since = int(_time.time() - days * 86400) if days else 0

    if days:
        # Time-filtered: rank by XP earned in the window from xp_events
        top_rows = conn.execute(
            """
            SELECT e.user_id, COALESCE(m.level, 0), SUM(e.amount) AS period_xp
            FROM xp_events e
            LEFT JOIN member_xp m ON m.guild_id = e.guild_id AND m.user_id = e.user_id
            WHERE e.guild_id = ? AND e.created_at >= ?
            GROUP BY e.user_id
            ORDER BY period_xp DESC
            LIMIT ?
            """,
            (guild_id, since, limit),
        ).fetchall()
    else:
        # All-time: use cumulative member_xp
        top_rows = conn.execute(
            """
            SELECT user_id, level, total_xp
            FROM member_xp
            WHERE guild_id = ?
            ORDER BY total_xp DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()

    user_ids = [int(r[0]) for r in top_rows]
    # XP by source for top users
    xp_by_source: dict[int, dict[str, float]] = {}
    if user_ids:
        ph = ",".join("?" * len(user_ids))
        time_clause = f" AND created_at >= {since}" if days else ""
        src_rows = conn.execute(
            f"""
            SELECT user_id, source, SUM(amount)
            FROM xp_events
            WHERE guild_id = ? AND user_id IN ({ph}){time_clause}
            GROUP BY user_id, source
            """,
            [guild_id, *user_ids],
        ).fetchall()
        for r in src_rows:
            xp_by_source.setdefault(int(r[0]), {})[str(r[1])] = float(r[2])

    leaderboard: list[XpUserRow] = []
    for r in top_rows:
        uid = int(r[0])
        sources = xp_by_source.get(uid, {})
        leaderboard.append(
            {
                "user_id": str(uid),
                "user_name": "",
                "level": int(r[1]),
                "total_xp": float(r[2]),
                "text_xp": sources.get("text", 0.0),
                "voice_xp": sources.get("voice", 0.0),
                "reply_xp": sources.get("reply", 0.0),
                "react_xp": sources.get("image_react", 0.0),
            }
        )

    # Level distribution (always all-time)
    level_rows = conn.execute(
        """
        SELECT level, COUNT(*) FROM member_xp
        WHERE guild_id = ?
        GROUP BY level
        ORDER BY level
        """,
        (guild_id,),
    ).fetchall()
    level_distribution: list[XpLevelBucket] = [
        {"level": int(r[0]), "count": int(r[1])} for r in level_rows
    ]

    # Source totals (filtered if days set)
    if days:
        source_rows = conn.execute(
            """
            SELECT source, SUM(amount) FROM xp_events
            WHERE guild_id = ? AND created_at >= ?
            GROUP BY source
            """,
            (guild_id, since),
        ).fetchall()
    else:
        source_rows = conn.execute(
            """
            SELECT source, SUM(amount) FROM xp_events
            WHERE guild_id = ?
            GROUP BY source
            """,
            (guild_id,),
        ).fetchall()
    source_totals = {str(r[0]): float(r[1]) for r in source_rows}

    total_row = conn.execute(
        "SELECT COUNT(*) FROM member_xp WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()

    return {
        "total_users": int(total_row[0]) if total_row else 0,
        "leaderboard": leaderboard,
        "level_distribution": level_distribution,
        "source_totals": source_totals,
    }


# ---------------------------------------------------------------------------
# Reaction analytics
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Message rate drops
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Burst ranking
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Channel comparison
# ---------------------------------------------------------------------------


class ChannelRow(TypedDict):
    channel_id: str
    channel_name: str
    message_count: int
    unique_authors: int
    recent_count: int
    prev_count: int
    trend_pct: float
    total_xp: float
    gini: float
    avg_sentiment: float | None


class ChannelComparisonData(TypedDict):
    channels: list[ChannelRow]


def get_channel_comparison_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int = 30,
    include_bots: bool = False,
    resolver: ChannelResolver | None = None,
) -> ChannelComparisonData:
    """Side-by-side channel metrics for the window.

    Every query below groups by the raw ``channel_id``, which for a message
    posted in a thread is the thread's own id. *resolver* folds those groups
    onto the channel each one belongs to — a thread onto its parent — and drops
    the ids that aren't channels at all (see services/channel_rollup). Passing
    None builds one from the database alone, which is the degraded mode used
    when the bot's guild cache isn't available.

    The folding is why author counts and sentiment are recombined here rather
    than taken from SQL: a thread and its parent share authors, so summing
    per-channel distinct counts would double-count anyone who posted in both,
    and averaging two averages would weight a three-message thread the same as
    its thousand-message parent.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    cutoff = now - days * 86400
    mid = now - days * 86400 // 2  # midpoint for trend
    bot_clause, bot_params = bot_filter_clause(guild_id, include_bots=include_bots)
    msg_bot_clause, msg_bot_params = bot_filter_clause(
        guild_id, column="m.author_id", include_bots=include_bots
    )
    if resolver is None:
        resolver = build_resolver(conn, guild_id, live_channel_ids=None)

    rows = conn.execute(
        f"""
        SELECT channel_id,
            COUNT(*) AS total,
            COUNT(DISTINCT author_id) AS authors,
            SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS recent,
            SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END) AS prev
        FROM messages
        WHERE guild_id = ? AND ts >= ?{bot_clause}
        GROUP BY channel_id
        ORDER BY total DESC
        """,
        (mid, mid, guild_id, cutoff, *bot_params),
    ).fetchall()

    # XP earned per channel in the window
    xp_rows = conn.execute(
        """
        SELECT channel_id, SUM(amount) AS xp
        FROM xp_events
        WHERE guild_id = ? AND created_at >= ?
        GROUP BY channel_id
        """,
        (guild_id, cutoff),
    ).fetchall()
    xp_by_channel: dict[int, float] = defaultdict(float)
    for r in xp_rows:
        if r[0] is None:
            continue
        target = resolver.resolve(int(r[0]))
        if target is not None:
            xp_by_channel[target] += float(r[1])

    # Per-channel Gini: message distribution among authors
    author_msg_rows = conn.execute(
        f"""
        SELECT channel_id, author_id, COUNT(*) AS cnt
        FROM messages
        WHERE guild_id = ? AND ts >= ?{bot_clause}
        GROUP BY channel_id, author_id
        """,
        (guild_id, cutoff, *bot_params),
    ).fetchall()
    # Keyed per author, not appended as a flat list: a member posting in both a
    # thread and its parent is one author of the merged channel, and their
    # messages belong in one bucket for the Gini spread.
    ch_author_counts: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for r in author_msg_rows:
        target = resolver.resolve(int(r[0]))
        if target is not None:
            ch_author_counts[target][int(r[1])] += float(r[2])

    def _gini(values: list[float]) -> float:
        if not values:
            return 0.0
        vals = sorted(values)
        n = len(vals)
        s = sum(vals)
        if s == 0:
            return 0.0
        cumsum = 0.0
        for i, v in enumerate(vals):
            cumsum += v * (2 * (i + 1) - n - 1)
        return cumsum / (n * s)

    # Average sentiment per channel. Summed rather than averaged in SQL so the
    # fold below can weight each thread by how much was actually said in it.
    sentiment_rows = conn.execute(
        f"""
        SELECT m.channel_id, SUM(ms.sentiment) AS total_s, COUNT(ms.sentiment) AS n
        FROM message_sentiment ms
        JOIN messages m ON ms.message_id = m.message_id
        WHERE ms.guild_id = ? AND m.ts >= ?{msg_bot_clause}
        GROUP BY m.channel_id
        """,
        (guild_id, cutoff, *msg_bot_params),
    ).fetchall()
    sentiment_totals: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in sentiment_rows:
        if r[1] is None or not r[2]:
            continue
        target = resolver.resolve(int(r[0]))
        if target is None:
            continue
        acc = sentiment_totals[target]
        acc[0] += float(r[1])
        acc[1] += float(r[2])
    sentiment_by_channel: dict[int, float | None] = {
        cid: round(total / n, 3) if n else None
        for cid, (total, n) in sentiment_totals.items()
    }

    # Fold the raw per-id volumes onto their resolved channel. The SQL's
    # ORDER BY no longer decides the final order — a channel can outrank
    # another only after its threads have been added in — so sort after.
    volumes: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])  # total, recent, prev
    for r in rows:
        target = resolver.resolve(int(r[0]))
        if target is None:
            continue
        acc = volumes[target]
        acc[0] += int(r[1])
        acc[1] += int(r[3])
        acc[2] += int(r[4])

    channels: list[ChannelRow] = []
    for cid, (total, recent, prev) in volumes.items():
        trend = (
            round((recent - prev) / max(prev, 1) * 100, 1)
            if prev > 0
            else (100.0 if recent > 0 else 0.0)
        )
        by_author = ch_author_counts.get(cid, {})
        gini_val = round(_gini(list(by_author.values())), 3)
        channels.append(
            {
                "channel_id": str(cid),
                "channel_name": "",
                "message_count": total,
                "unique_authors": len(by_author),
                "recent_count": recent,
                "prev_count": prev,
                "trend_pct": trend,
                "total_xp": xp_by_channel.get(cid, 0.0),
                "gini": gini_val,
                "avg_sentiment": sentiment_by_channel.get(cid),
            }
        )

    channels.sort(key=lambda c: c["message_count"], reverse=True)
    return {"channels": channels}


# ---------------------------------------------------------------------------
# Animated interaction heatmap
# ---------------------------------------------------------------------------


def get_one_sided_attention_data(
    conn: sqlite3.Connection,
    guild_id: int,
    window_days: int = 30,
    limit: int = 50,
) -> dict:
    """Moderator-review report of lopsided attention between member pairs.

    Thin adapter over attention_report.compute_one_sided_attention: runs the
    metrics, then serialises each candidate for the web layer with both user
    IDs stringified (JS Number precision — see snowflake convention).
    """
    from bot_modules.services.attention_report import compute_one_sided_attention

    # Bots are false positives here — a member replying to / reacting to a bot,
    # or "following" one into voice, is not a one-sided relationship. Allowlisted
    # bots live in known_users with is_bot=1; feed them to the report's own
    # exclusion so neither endpoint of a flagged pair can be a bot.
    bot_ids = {
        int(r[0])
        for r in conn.execute(
            "SELECT user_id FROM known_users WHERE guild_id = ? AND is_bot = 1",
            (guild_id,),
        )
    }

    candidates = compute_one_sided_attention(
        conn, guild_id, window_days=window_days, limit=limit, exclude_ids=bot_ids
    )
    rows = [
        {
            "from_id": str(c.initiator_id),
            "from_name": "",
            "to_id": str(c.target_id),
            "to_name": "",
            "text_out": c.text_out,
            "react_out": c.react_out,
            "voice_follow_out": c.voice_follow_out,
            "weight_out": round(c.weight_out, 1),
            "weight_back": round(c.weight_back, 1),
            "approach_out": round(c.approach_out, 1),
            "asymmetry": round(c.asymmetry, 3),
            "target_reciprocation_rate": round(c.target_reciprocation_rate, 2),
            "expected_back": round(c.expected_back, 1),
            "reciprocation_shortfall": round(c.reciprocation_shortfall, 3),
            "concentration": round(c.concentration, 3),
            "distinct_targets": c.distinct_targets,
            "escalation": (round(c.escalation, 2) if c.escalation is not None else None),
            "ever_reciprocated": c.ever_reciprocated,
            "max_burst": c.max_burst,
            "reasons": list(c.reasons),
            "cautions": list(c.cautions),
        }
        for c in candidates
    ]
    return {"window_days": window_days, "candidates": rows}
