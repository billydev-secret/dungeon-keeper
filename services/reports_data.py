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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypedDict

from services.activity_graphs import (
    Resolution,
    query_burst_ranking,
    query_dropoff_profiles,
    query_message_activity,
    query_message_cadence,
    query_message_histogram,
    query_message_rate_10min,
    query_message_rate_drops,
    query_nsfw_gender_activity,
    query_role_growth,
    query_xp_activity,
    query_xp_histogram,
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

_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_HOD_LABELS = [
    "12am", "1am", "2am", "3am", "4am", "5am", "6am", "7am",
    "8am", "9am", "10am", "11am", "12pm", "1pm", "2pm", "3pm",
    "4pm", "5pm", "6pm", "7pm", "8pm", "9pm", "10pm", "11pm",
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

class RoleGrowthSeries(TypedDict):
    role: str
    counts: list[int]


class RoleGrowthData(TypedDict):
    resolution: str
    window_label: str
    labels: list[str]
    series: list[RoleGrowthSeries]


def get_role_growth_data(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    role_filter: set[str] | None,
    utc_offset_hours: float = 0,
) -> RoleGrowthData:
    labels, role_counts = query_role_growth(conn, guild_id, resolution, utc_offset_hours=utc_offset_hours)

    if role_filter is not None:
        role_counts = {
            name: counts
            for name, counts in role_counts.items()
            if name.lower() in role_filter
        }

    series: list[RoleGrowthSeries] = [
        {"role": name, "counts": counts} for name, counts in role_counts.items()
    ]

    return {
        "resolution": resolution,
        "window_label": _WINDOW_LABELS.get(resolution, resolution),
        "labels": labels,
        "series": series,
    }


# ---------------------------------------------------------------------------
# Message cadence
# ---------------------------------------------------------------------------

class CadenceBucketDict(TypedDict):
    label: str
    min_gap: float
    p20_gap: float
    median_gap: float
    p80_gap: float
    max_gap: float


class MessageCadenceData(TypedDict):
    resolution: str
    window_label: str
    channel_id: str | None
    buckets: list[CadenceBucketDict]


def get_message_cadence_data(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    utc_offset_hours: float,
    channel_id: int | None,
) -> MessageCadenceData:
    buckets = query_message_cadence(
        conn, guild_id, resolution,
        utc_offset_hours=utc_offset_hours,
        channel_id=channel_id,
    )
    return {
        "resolution": resolution,
        "window_label": _WINDOW_LABELS.get(resolution, resolution),
        "channel_id": str(channel_id) if channel_id else None,
        "buckets": [
            {
                "label": b.label,
                "min_gap": b.min_gap,
                "p20_gap": b.p20_gap,
                "median_gap": b.median_gap,
                "p80_gap": b.p80_gap,
                "max_gap": b.max_gap,
            }
            for b in buckets
        ],
    }


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
) -> NsfwGenderData:
    labels, gender_counts = query_nsfw_gender_activity(
        conn, guild_id, resolution, channel_ids,
        utc_offset_hours=utc_offset_hours,
        media_only=media_only,
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

class MessageRateData(TypedDict):
    days: int
    tz_label: str
    buckets: list[int]
    avg_per_day: list[float]


def get_message_rate_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int,
    utc_offset_hours: float,
) -> MessageRateData:
    counts = query_message_rate_10min(
        conn, guild_id, days, utc_offset_hours=utc_offset_hours,
    )
    tz_label = f"UTC{utc_offset_hours:+g}" if utc_offset_hours else "UTC"
    return {
        "days": days,
        "tz_label": tz_label,
        "buckets": counts,
        "avg_per_day": [c / days for c in counts],
    }


# ---------------------------------------------------------------------------
# Greeter response times
# ---------------------------------------------------------------------------

class ResponseBucket(TypedDict):
    label: str
    count: int


class GreeterResponseEntry(TypedDict):
    user_id: str
    joined_at: float
    response_seconds: float
    greeter_id: str


class GreeterResponseData(TypedDict):
    window_label: str
    count: int
    median_seconds: float
    mean_seconds: float
    histogram: list[ResponseBucket]
    response_times_seconds: list[float]
    entries: list[GreeterResponseEntry]


def _query_greeter_response_details(
    conn: sqlite3.Connection,
    guild_id: int,
    welcome_channel_id: int,
    greeter_ids: set[int],
    join_times: dict[int, float],
) -> list[GreeterResponseEntry]:
    """Match each joiner to the first available greeter message after their join.

    Uses greedy 1:1 matching — each greeter message can only be claimed by one
    joiner (the earliest unmatched joiner it follows). A greeter's own join is
    never matched to their own message.
    """
    if not greeter_ids or not join_times:
        return []

    placeholders = ",".join("?" * len(greeter_ids))
    greeter_msgs = conn.execute(
        f"""
        SELECT author_id, ts FROM messages
        WHERE guild_id = ? AND channel_id = ?
          AND author_id IN ({placeholders})
        ORDER BY ts
        """,
        (guild_id, welcome_channel_id, *greeter_ids),
    ).fetchall()

    if not greeter_msgs:
        return []

    greeter_times = [(int(r[0]), int(r[1])) for r in greeter_msgs]

    # Sort joins chronologically for greedy matching
    sorted_joins = sorted(join_times.items(), key=lambda kv: kv[1])

    entries: list[GreeterResponseEntry] = []
    msg_idx = 0  # next unconsumed greeter message

    for user_id, joined_at in sorted_joins:
        # Advance past any greeter messages before this join
        while msg_idx < len(greeter_times) and greeter_times[msg_idx][1] < joined_at:
            msg_idx += 1

        # Find the next unconsumed greeter message NOT authored by the joiner
        scan = msg_idx
        while scan < len(greeter_times):
            author_id, msg_ts = greeter_times[scan]
            if author_id != user_id:
                delta = msg_ts - joined_at
                entries.append({
                    "user_id": str(user_id),
                    "joined_at": joined_at,
                    "response_seconds": delta,
                    "greeter_id": str(author_id),
                })
                # Consume this message and all prior ones up to it
                msg_idx = scan + 1
                break
            scan += 1

    return entries


def get_greeter_response_data(
    conn: sqlite3.Connection,
    guild_id: int,
    welcome_channel_id: int,
    greeter_ids: set[int],
    join_times: dict[int, float],
) -> GreeterResponseData:
    entries = _query_greeter_response_details(
        conn, guild_id, welcome_channel_id, greeter_ids, join_times,
    )
    response_times = [e["response_seconds"] for e in entries]

    bucket_counts = [0] * len(_RESPONSE_BUCKETS)
    for t in response_times:
        for i, (threshold, _) in enumerate(_RESPONSE_BUCKETS):
            if t < threshold:
                bucket_counts[i] += 1
                break

    med = statistics.median(response_times) if response_times else 0.0
    avg = statistics.mean(response_times) if response_times else 0.0

    entries.sort(key=lambda e: e["joined_at"], reverse=True)

    return {
        "window_label": "All Time",
        "count": len(response_times),
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

class ActivityData(TypedDict):
    resolution: str
    window_label: str
    mode: str
    labels: list[str]
    counts: list[float]
    member_counts: list[int]
    show_members: bool
    y_label: str
    tz_label: str


def get_activity_data(
    conn: sqlite3.Connection,
    guild_id: int,
    resolution: Resolution,
    utc_offset_hours: float,
    mode: Literal["messages", "xp"] = "messages",
    user_id: int | None = None,
    channel_id: int | None = None,
) -> ActivityData:
    tz_label = f"UTC{utc_offset_hours:+g}" if utc_offset_hours else "UTC"
    show_members = user_id is None

    if mode == "xp":
        if resolution in ("hour_of_day", "day_of_week"):
            labels, xp_totals = query_xp_histogram(
                conn, guild_id, resolution,  # type: ignore[arg-type]
                user_id=user_id, channel_id=channel_id,
                utc_offset_hours=utc_offset_hours,
            )
            counts: list[float] = xp_totals
            member_counts: list[int] = []
            show_members = False
        else:
            labels, xp_totals, member_counts = query_xp_activity(
                conn, guild_id, resolution,
                user_id=user_id, channel_id=channel_id,
                utc_offset_hours=utc_offset_hours,
            )
            counts = xp_totals
        y_label = "XP Earned"
    else:
        if resolution in ("hour_of_day", "day_of_week"):
            labels, msg_counts = query_message_histogram(
                conn, guild_id, resolution,  # type: ignore[arg-type]
                user_id=user_id, channel_id=channel_id,
                utc_offset_hours=utc_offset_hours,
            )
            counts = [float(c) for c in msg_counts]
            member_counts = []
            show_members = False
        else:
            labels, msg_counts, member_counts = query_message_activity(
                conn, guild_id, resolution,
                user_id=user_id, channel_id=channel_id,
                utc_offset_hours=utc_offset_hours,
            )
            counts = [float(c) for c in msg_counts]
        y_label = "Messages"

    return {
        "resolution": resolution,
        "window_label": _WINDOW_LABELS.get(resolution, resolution),
        "mode": mode,
        "labels": labels,
        "counts": counts,
        "member_counts": member_counts,
        "show_members": show_members,
        "y_label": y_label,
        "tz_label": tz_label,
    }


# ---------------------------------------------------------------------------
# Invite effectiveness
# ---------------------------------------------------------------------------

class InviterRow(TypedDict):
    inviter_id: str
    inviter_name: str
    invite_count: int
    still_active: int
    retention_pct: float


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
            "total_invites": 0, "total_active": 0,
            "overall_retention_pct": 0.0, "inviters": [],
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
        inviters.append({
            "inviter_id": str(inviter_id),
            "inviter_name": "",
            "invite_count": count,
            "still_active": active,
            "retention_pct": round(active / count * 100, 1) if count else 0.0,
        })

    inviters.sort(key=lambda r: r["invite_count"], reverse=True)

    return {
        "total_invites": total_invites,
        "total_active": total_active,
        "overall_retention_pct": round(total_active / total_invites * 100, 1) if total_invites else 0.0,
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


class InteractionGraphData(TypedDict):
    nodes: list[InteractionNode]
    edges: list[InteractionEdge]
    top_pairs: list[InteractionEdge]


def get_interaction_graph_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int | None = None,
    limit: int = 50,
) -> InteractionGraphData:
    now = int(datetime.now(timezone.utc).timestamp())

    if days is not None:
        cutoff = now - days * 86400
        rows = conn.execute(
            """
            SELECT from_user_id, to_user_id, COUNT(*) as weight
            FROM user_interactions_log
            WHERE guild_id = ? AND ts >= ?
            GROUP BY from_user_id, to_user_id
            ORDER BY weight DESC
            """,
            (guild_id, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT from_user_id, to_user_id, weight
            FROM user_interactions
            WHERE guild_id = ?
            ORDER BY weight DESC
            """,
            (guild_id,),
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
        edges.append({
            "from_id": str(from_id), "from_name": "",
            "to_id": str(to_id), "to_name": "",
            "weight": weight,
        })

    # Top pairs: merge bidirectional
    pair_weights: dict[tuple[int, int], int] = {}
    for r in rows:
        a, b, w = int(r[0]), int(r[1]), int(r[2])
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        pair_weights[key] = pair_weights.get(key, 0) + w
    top_pairs: list[InteractionEdge] = []
    for (a, b), w in sorted(pair_weights.items(), key=lambda x: x[1], reverse=True)[:limit]:
        top_pairs.append({
            "from_id": str(a), "from_name": "",
            "to_id": str(b), "to_name": "",
            "weight": w,
        })

    all_ids = set(node_out.keys()) | set(node_in.keys())
    nodes: list[InteractionNode] = []
    for uid in sorted(all_ids, key=lambda u: node_out.get(u, 0) + node_in.get(u, 0), reverse=True)[:limit]:
        nodes.append({
            "user_id": str(uid), "user_name": "",
            "total_outbound": node_out.get(uid, 0),
            "total_inbound": node_in.get(uid, 0),
            "unique_partners": len(node_partners.get(uid, set())),
        })

    return {
        "nodes": nodes,
        "edges": edges[:limit * 2],
        "top_pairs": top_pairs,
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
    days_active_prev: int
    days_active_recent: int
    last_seen_ts: float | None
    level: int
    total_xp: float


class RetentionData(TypedDict):
    period_days: int
    total_dropoffs: int
    entries: list[RetentionEntry]


def get_retention_data(
    conn: sqlite3.Connection,
    guild_id: int,
    period_days: int = 30,
    min_previous: int = 5,
    limit: int = 25,
) -> RetentionData:
    period_seconds = period_days * 86400
    profiles = query_dropoff_profiles(
        conn, guild_id, period_seconds,
        min_previous=min_previous, limit=limit,
    )

    entries: list[RetentionEntry] = []
    for p in profiles:
        drop_pct = round((1 - p.msgs_recent / max(p.msgs_prev, 1)) * 100, 1)
        entries.append({
            "user_id": str(p.user_id),
            "user_name": "",
            "msgs_prev": p.msgs_prev,
            "msgs_recent": p.msgs_recent,
            "drop_pct": drop_pct,
            "days_active_prev": p.days_prev,
            "days_active_recent": p.days_recent,
            "last_seen_ts": p.last_seen_ts,
            "level": p.level,
            "total_xp": p.total_xp,
        })

    return {
        "period_days": period_days,
        "total_dropoffs": len(entries),
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
        top_users.append({
            "user_id": str(uid),
            "user_name": "",
            "total_minutes": float(minutes),
            "session_count": sessions,
            "avg_minutes": round(minutes / max(sessions, 1), 1),
        })

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
        by_hour.append({
            "hour": h,
            "label": _HOD_LABELS[h],
            "total_minutes": float(hour_map.get(h, 0)),
        })

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
        leaderboard.append({
            "user_id": str(uid),
            "user_name": "",
            "level": int(r[1]),
            "total_xp": float(r[2]),
            "text_xp": sources.get("text", 0.0),
            "voice_xp": sources.get("voice", 0.0),
            "reply_xp": sources.get("reply", 0.0),
            "react_xp": sources.get("image_react", 0.0),
        })

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
        "SELECT COUNT(*) FROM member_xp WHERE guild_id = ?", (guild_id,),
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

class EmojiRow(TypedDict):
    emoji: str
    total_count: int


class ReactionUserRow(TypedDict):
    user_id: str
    user_name: str
    given: int
    received: int


class ReactionAnalyticsData(TypedDict):
    top_emoji: list[EmojiRow]
    top_givers: list[ReactionUserRow]
    top_receivers: list[ReactionUserRow]
    total_reactions: int


def get_reaction_analytics_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int | None = None,
    limit: int = 20,
) -> ReactionAnalyticsData:
    now = int(datetime.now(timezone.utc).timestamp())

    cutoff_clause = ""
    params: list[object] = [guild_id]
    if days is not None:
        cutoff_clause = "AND ts >= ?"
        params.append(now - days * 86400)

    # Top emoji — use message_reactions counts, filtered to messages
    # in this guild (and optionally time window) via the messages table.
    emoji_cutoff = ""
    emoji_params: list[object] = [guild_id]
    if days is not None:
        emoji_cutoff = "AND m.ts >= ?"
        emoji_params.append(now - days * 86400)
    emoji_rows = conn.execute(
        f"""
        SELECT mr.emoji, SUM(mr.count) AS cnt
        FROM message_reactions mr
        JOIN messages m ON m.message_id = mr.message_id
        WHERE m.guild_id = ? {emoji_cutoff}
        GROUP BY mr.emoji
        ORDER BY cnt DESC
        LIMIT ?
        """,
        [*emoji_params, limit],
    ).fetchall()
    top_emoji: list[EmojiRow] = [
        {"emoji": str(r[0]), "total_count": int(r[1])} for r in emoji_rows
    ]

    # Top givers
    giver_rows = conn.execute(
        f"""
        SELECT reactor_id, COUNT(*) AS cnt
        FROM reaction_log
        WHERE guild_id = ? {cutoff_clause}
        GROUP BY reactor_id
        ORDER BY cnt DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    # Top receivers
    receiver_rows = conn.execute(
        f"""
        SELECT author_id, COUNT(*) AS cnt
        FROM reaction_log
        WHERE guild_id = ? {cutoff_clause}
        GROUP BY author_id
        ORDER BY cnt DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    # Total reactions from message_reactions (accurate aggregate counts)
    total_row = conn.execute(
        f"""
        SELECT COALESCE(SUM(mr.count), 0)
        FROM message_reactions mr
        JOIN messages m ON m.message_id = mr.message_id
        WHERE m.guild_id = ? {emoji_cutoff}
        """,
        emoji_params,
    ).fetchone()

    top_givers: list[ReactionUserRow] = [
        {"user_id": str(r[0]), "user_name": "", "given": int(r[1]), "received": 0}
        for r in giver_rows
    ]
    top_receivers: list[ReactionUserRow] = [
        {"user_id": str(r[0]), "user_name": "", "given": 0, "received": int(r[1])}
        for r in receiver_rows
    ]

    return {
        "top_emoji": top_emoji,
        "top_givers": top_givers,
        "top_receivers": top_receivers,
        "total_reactions": int(total_row[0]) if total_row else 0,
    }


# ---------------------------------------------------------------------------
# Message rate drops
# ---------------------------------------------------------------------------

class RateDropEntry(TypedDict):
    user_id: str
    user_name: str
    prev_count: int
    recent_count: int
    drop_pct: float
    adjusted_drop_pct: float


class MessageRateDropsData(TypedDict):
    period_days: int
    server_prev: int
    server_recent: int
    server_drop_pct: float
    entries: list[RateDropEntry]


def get_message_rate_drops_data(
    conn: sqlite3.Connection,
    guild_id: int,
    period_days: int = 14,
    min_previous: int = 5,
    limit: int = 25,
) -> MessageRateDropsData:
    period_seconds = period_days * 86400
    now = int(datetime.now(timezone.utc).timestamp())
    mid = now - int(period_seconds)
    start = mid - int(period_seconds)

    # Server-wide totals for normalization
    srv_row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN created_at < ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)
        FROM processed_messages
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
        """,
        [mid, mid, guild_id, start, now],
    ).fetchone()
    server_prev = int(srv_row[0] or 0) if srv_row else 0
    server_recent = int(srv_row[1] or 0) if srv_row else 0
    server_ratio = server_recent / max(server_prev, 1)
    server_drop_pct = round((1 - server_ratio) * 100, 1)

    drops = query_message_rate_drops(
        conn, guild_id, period_seconds,
        min_previous=min_previous, limit=limit,
    )

    entries: list[RateDropEntry] = []
    for user_id, prev_count, recent_count in drops:
        drop_pct = round((1 - recent_count / max(prev_count, 1)) * 100, 1)
        # Adjusted: what we'd expect if user followed server trend
        expected = prev_count * server_ratio
        adjusted = round((1 - recent_count / max(expected, 1)) * 100, 1)
        entries.append({
            "user_id": str(user_id),
            "user_name": "",
            "prev_count": prev_count,
            "recent_count": recent_count,
            "drop_pct": drop_pct,
            "adjusted_drop_pct": adjusted,
        })

    return {
        "period_days": period_days,
        "server_prev": server_prev,
        "server_recent": server_recent,
        "server_drop_pct": server_drop_pct,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Burst ranking
# ---------------------------------------------------------------------------

class BurstEntry(TypedDict):
    user_id: str
    user_name: str
    pre_avg: float
    post_avg: float
    increase: float
    sessions: int


class BurstRankingData(TypedDict):
    entries: list[BurstEntry]


def get_burst_ranking_data(
    conn: sqlite3.Connection,
    guild_id: int,
    min_sessions: int = 3,
    days: int | None = None,
    limit: int = 25,
) -> BurstRankingData:
    results = query_burst_ranking(conn, guild_id, min_sessions=min_sessions, days=days)

    entries: list[BurstEntry] = []
    for user_id, pre_avg, post_avg, n_sessions in results[:limit]:
        entries.append({
            "user_id": str(user_id),
            "user_name": "",
            "pre_avg": round(pre_avg, 2),
            "post_avg": round(post_avg, 2),
            "increase": round(post_avg - pre_avg, 2),
            "sessions": n_sessions,
        })

    return {"entries": entries}


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


class ChannelComparisonData(TypedDict):
    channels: list[ChannelRow]


def get_channel_comparison_data(
    conn: sqlite3.Connection,
    guild_id: int,
    days: int = 30,
) -> ChannelComparisonData:
    now = int(datetime.now(timezone.utc).timestamp())
    cutoff = now - days * 86400
    mid = now - days * 86400 // 2  # midpoint for trend

    rows = conn.execute(
        """
        SELECT channel_id,
            COUNT(*) AS total,
            COUNT(DISTINCT author_id) AS authors,
            SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS recent,
            SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END) AS prev
        FROM messages
        WHERE guild_id = ? AND ts >= ?
        GROUP BY channel_id
        ORDER BY total DESC
        """,
        (mid, mid, guild_id, cutoff),
    ).fetchall()

    channels: list[ChannelRow] = []
    for r in rows:
        recent = int(r[3])
        prev = int(r[4])
        trend = round((recent - prev) / max(prev, 1) * 100, 1) if prev > 0 else (100.0 if recent > 0 else 0.0)
        channels.append({
            "channel_id": str(r[0]),
            "channel_name": "",
            "message_count": int(r[1]),
            "unique_authors": int(r[2]),
            "recent_count": recent,
            "prev_count": prev,
            "trend_pct": trend,
        })

    return {"channels": channels}
