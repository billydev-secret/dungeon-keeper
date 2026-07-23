"""Member Quality Score — identifies genuinely disengaged server members.

Produces a ranked list with four component scores:
  1. Engagement Given (40%) — reactions given + replies sent to others
  2. Consistency & Recency (25%) — exponential decay + weekly regularity
  3. Content Resonance (20%) — reactions/replies received per post
  4. Posting Activity (15%) — attachments + conversation starters

Members never see scores. Output is mod-only.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from bot_modules.services.gender_service import get_gender_map

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_DAYS = 90
ONBOARDING_DAYS = 7
NEW_MEMBER_DAYS = 30  # starred in reports — scores normalized but based on less data
MIN_ACTIVE_DAYS = 7
WEEKS_IN_WINDOW = 13

# Component weights
W_ENGAGEMENT = 0.40
W_CONSISTENCY = 0.25
W_RESONANCE = 0.20
W_POSTING = 0.15

# Recency decay
DECAY_RATE = 0.05

# Anti-gaming
REACTION_SAME_PERSON_HALF = 5
REACTION_SAME_PERSON_CAP = 10
MIN_REPLY_CHARS = 5
ATTACHMENT_DAILY_CAP = 8
STARTER_DAILY_CAP = 15

# Tenure buffer (days added to inactivity threshold)
TENURE_6MO_DAYS = 182
TENURE_12MO_DAYS = 365
TENURE_6MO_BUFFER = 30
TENURE_12MO_BUFFER = 60

# Initiative multipliers
INITIATIVE_TIERS = [
    (0.60, 1.10),  # 60%+ initiated
    (0.40, 1.00),  # 40-60%
    (0.20, 0.92),  # 20-40%
    (0.00, 0.85),  # under 20%
]

# Status labels
STATUS_ACTIVE = "Active"
STATUS_ONBOARDING = "Onboarding"
STATUS_INSUFFICIENT = "Insufficient Data"
STATUS_LEAVE = "Leave of Absence"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MemberStandIn:
    """Duck-typed stand-in for discord.Member (id / bot / joined_at).

    Used where live Member objects aren't available: the offline dashboard
    path and the cache warmer's member snapshots.
    """

    id: int
    bot: bool
    joined_at: datetime | None


@dataclass
class QualityScore:
    user_id: int
    final_score: float
    engagement_given: float
    consistency_recency: float
    content_resonance: float
    posting_activity: float
    last_active_ts: float
    status: str
    tenure_buffer_days: int
    active_days: int
    active_weeks: int


# ---------------------------------------------------------------------------
# Leave of absence tables
# ---------------------------------------------------------------------------


def init_quality_score_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quality_score_leaves (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            start_ts    REAL NOT NULL,
            end_ts      REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


def add_leave(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    start_ts: float,
    end_ts: float,
) -> None:
    conn.execute(
        """
        INSERT INTO quality_score_leaves (guild_id, user_id, start_ts, end_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET start_ts=excluded.start_ts, end_ts=excluded.end_ts
        """,
        (guild_id, user_id, start_ts, end_ts),
    )


def remove_leave(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM quality_score_leaves WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def get_leaves(
    conn: sqlite3.Connection, guild_id: int
) -> dict[int, tuple[float, float]]:
    """Return {user_id: (start_ts, end_ts)} for all active leaves."""
    rows = conn.execute(
        "SELECT user_id, start_ts, end_ts FROM quality_score_leaves WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {int(r["user_id"]): (float(r["start_ts"]), float(r["end_ts"])) for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile_ranks(values: list[float]) -> list[float]:
    """Compute percentile rank (0-1) for each value in the list."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and values[indexed[j]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j - 1) / 2.0
        for k in range(i, j):
            ranks[indexed[k]] = avg_rank / (n - 1)
        i = j
    return ranks


def _day_key(ts: float) -> int:
    """Return day number (days since epoch) for a timestamp."""
    return int(ts) // 86400


def _week_key(ts: float) -> int:
    """Return ISO week number within the 90-day window."""
    return int(ts) // (7 * 86400)


def _fetch_tuples(conn: sqlite3.Connection, sql: str, params: tuple) -> list[tuple]:
    """Fetch as plain tuples regardless of the connection's row factory.

    sqlite3.Row construction is measurable at hundreds of thousands of rows;
    the bulk queries here only need positional access.
    """
    cur = conn.execute(sql, params)
    cur.row_factory = None  # type: ignore[assignment]
    return cur.fetchall()


def _initiative_multiplier(initiated_ratio: float) -> float:
    for threshold, mult in INITIATIVE_TIERS:
        if initiated_ratio >= threshold:
            return mult
    return 0.85


def _tenure_buffer(
    joined_at: datetime | None,
    now: datetime,
    months_active: int,
) -> int:
    """Compute tenure buffer days based on join date and historical activity.

    *months_active* is the member's count of distinct active months across all
    history, batch-fetched for every author at once in compute_quality_scores
    rather than one query per member.
    """
    if joined_at is None:
        return 0
    tenure_days = (now - joined_at).days
    if tenure_days < TENURE_6MO_DAYS:
        return 0

    # Check "consistent historical activity": at least 1 active week per month
    # for the majority of tenure.
    tenure_months = max(1, tenure_days // 30)
    if months_active < tenure_months * 0.5:
        return 0  # Not consistently active

    if tenure_days >= TENURE_12MO_DAYS:
        return TENURE_12MO_BUFFER
    return TENURE_6MO_BUFFER


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


def compute_quality_scores(
    conn: sqlite3.Connection,
    guild_id: int,
    members: Sequence[discord.Member],
    now: datetime | None = None,
    window_days: int | None = None,
    min_active_days: int | None = None,
) -> list[QualityScore]:
    """Compute quality scores for all provided members.

    Returns a list sorted by final_score descending, with non-scored members
    (onboarding, leave, insufficient data) at the end.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    _window_days = window_days if window_days is not None else WINDOW_DAYS
    _min_active_days = (
        min_active_days if min_active_days is not None else MIN_ACTIVE_DAYS
    )
    window_start = now_ts - (_window_days * 86400)

    # Fetch leaves
    leaves = get_leaves(conn, guild_id)

    # Classify members upfront
    member_map: dict[int, discord.Member] = {}
    scored_ids: list[int] = []
    results: list[QualityScore] = []

    for m in members:
        if m.bot:
            continue
        member_map[m.id] = m

        # Leave of absence
        if m.id in leaves:
            _start, end = leaves[m.id]
            if end >= now_ts:
                results.append(
                    QualityScore(
                        user_id=m.id,
                        final_score=0,
                        engagement_given=0,
                        consistency_recency=0,
                        content_resonance=0,
                        posting_activity=0,
                        last_active_ts=0,
                        status=STATUS_LEAVE,
                        tenure_buffer_days=0,
                        active_days=0,
                        active_weeks=0,
                    )
                )
                continue

        # Onboarding
        if m.joined_at and (now - m.joined_at).days < ONBOARDING_DAYS:
            results.append(
                QualityScore(
                    user_id=m.id,
                    final_score=0,
                    engagement_given=0,
                    consistency_recency=0,
                    content_resonance=0,
                    posting_activity=0,
                    last_active_ts=0,
                    status=STATUS_ONBOARDING,
                    tenure_buffer_days=0,
                    active_days=0,
                    active_weeks=0,
                )
            )
            continue

        scored_ids.append(m.id)

    if not scored_ids:
        return results

    # ------------------------------------------------------------------
    # Bulk data fetch
    # ------------------------------------------------------------------
    id_set = set(scored_ids)

    # Messages in window. Only the content *length* matters (reply-quality
    # check), so avoid hauling hundreds of MB of text out of SQLite.
    msg_rows = _fetch_tuples(
        conn,
        "SELECT message_id, author_id, ts, LENGTH(content), reply_to_id "
        "FROM messages WHERE guild_id = ? AND ts >= ? ORDER BY ts",
        (guild_id, int(window_start)),
    )

    # Attachment message IDs in window
    attach_rows = _fetch_tuples(
        conn,
        """
        SELECT DISTINCT ma.message_id
        FROM message_attachments ma
        JOIN messages m ON ma.message_id = m.message_id
        WHERE m.guild_id = ? AND m.ts >= ?
        """,
        (guild_id, int(window_start)),
    )
    attachment_msg_ids = {r[0] for r in attach_rows}

    # Reaction log (individual reactions given)
    react_rows = _fetch_tuples(
        conn,
        "SELECT reactor_id, author_id, ts FROM reaction_log "
        "WHERE guild_id = ? AND ts >= ?",
        (guild_id, int(window_start)),
    )

    # Reaction counts received (aggregate per message)
    reaction_count_rows = _fetch_tuples(
        conn,
        """
        SELECT mr.message_id, SUM(mr.count) AS total
        FROM message_reactions mr
        JOIN messages m ON mr.message_id = m.message_id
        WHERE m.guild_id = ? AND m.ts >= ?
        GROUP BY mr.message_id
        """,
        (guild_id, int(window_start)),
    )
    reaction_counts: dict[int, int] = {r[0]: r[1] for r in reaction_count_rows}

    # ------------------------------------------------------------------
    # Per-member aggregation
    # ------------------------------------------------------------------

    # Single pass over all message rows (the dominant data volume): build the
    # author map and per-author tuples, and set aside the much smaller reply
    # subset for the reply-derived lookups below.
    msgs_by_author: dict[int, list[tuple[int, int, int | None, int | None]]] = (
        defaultdict(list)
    )  # author -> [(message_id, ts, content_len, reply_to_id)]
    msg_author_map: dict[int, int] = {}  # message_id -> author_id
    reply_rows: list[tuple[int, int, int]] = []  # (ts, author_id, reply_to_id)
    for mid, uid, ts, content_len, reply_to in msg_rows:
        msg_author_map[mid] = uid
        if uid in id_set:
            msgs_by_author[uid].append((mid, ts, content_len, reply_to))
        if reply_to is not None:
            reply_rows.append((ts, uid, reply_to))

    # Reaction log data: outbound (reactor -> [(author, ts)]) and inbound
    # (author -> [(ts, reactor)]) built in one pass.
    reactions_given: dict[int, list[tuple[int, int]]] = defaultdict(list)
    reactions_to_user: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for reactor, author, ts in react_rows:
        if reactor in id_set:
            reactions_given[reactor].append((author, ts))
        if reactor != author:
            reactions_to_user[author].append((ts, reactor))

    # Reply-derived lookups: replies per message (content resonance) and
    # replies received per author (initiative reverse-engagement).
    replies_per_msg: dict[int, int] = defaultdict(int)
    replies_to_user: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for ts, uid, reply_to in reply_rows:
        replies_per_msg[reply_to] += 1
        target_uid = msg_author_map.get(reply_to)
        if target_uid is not None and target_uid != uid:
            replies_to_user[target_uid].append((ts, uid))

    # ------------------------------------------------------------------
    # Compute raw metrics per member
    # ------------------------------------------------------------------

    # Metric arrays (parallel to scored_ids)
    n = len(scored_ids)
    reaction_rates = [0.0] * n
    reply_ratios = [0.0] * n
    initiative_ratios = [0.5] * n
    recency_values = [0.0] * n
    consistency_values = [0.0] * n
    resonance_values = [0.0] * n
    posting_rates = [0.0] * n
    last_active = [0.0] * n
    active_days_arr = [0] * n
    active_weeks_arr = [0] * n

    for idx, uid in enumerate(scored_ids):
        user_msgs = msgs_by_author.get(uid, [])
        user_reactions = reactions_given.get(uid, [])

        # One pass over this member's messages: activity days/weeks, last-seen,
        # reply quality, outbound engagement events, posts, and posting caps.
        activity_days: set[int] = set()
        activity_weeks: set[int] = set()
        last_ts = 0.0
        qualifying_replies = 0
        engagement_events: list[tuple[int, int]] = []  # (ts, target_uid)
        posts_count = 0
        posts_resonance = 0.0
        daily_attachments: dict[int, int] = defaultdict(int)
        daily_starters: dict[int, int] = defaultdict(int)

        for mid, ts, content_len, reply_to in user_msgs:
            day = ts // 86400
            activity_days.add(day)
            activity_weeks.add(ts // 604800)
            if ts > last_ts:
                last_ts = ts
            is_attachment = mid in attachment_msg_ids
            if is_attachment:
                daily_attachments[day] += 1
            if reply_to is not None:
                if content_len is not None and content_len >= MIN_REPLY_CHARS:
                    qualifying_replies += 1
                reply_target = msg_author_map.get(reply_to)
                if reply_target is not None and reply_target != uid:
                    engagement_events.append((ts, reply_target))
                is_post = is_attachment
            else:
                daily_starters[day] += 1
                is_post = True
            if is_post:
                posts_count += 1
                posts_resonance += reaction_counts.get(mid, 0) + replies_per_msg.get(
                    mid, 0
                )

        # One pass over reactions given: activity, anti-gaming tally, outbound
        # engagement events (appended after message events, order restored by
        # the sort below just as before).
        reaction_target_days: dict[tuple[int, int], int] = defaultdict(
            int
        )  # (day, target) -> count
        for target, ts in user_reactions:
            day = ts // 86400
            activity_days.add(day)
            activity_weeks.add(ts // 604800)
            if ts > last_ts:
                last_ts = ts
            reaction_target_days[(day, target)] += 1
            if target != uid:
                engagement_events.append((ts, target))
        engagement_events.sort()

        n_active_days = len(activity_days)
        n_active_weeks = len(activity_weeks)
        active_days_arr[idx] = n_active_days
        active_weeks_arr[idx] = n_active_weeks
        last_active[idx] = last_ts

        # -- Engagement Given --

        # Reaction rate (per active day), with anti-gaming same-person caps
        reaction_credit = 0.0
        for count in reaction_target_days.values():
            if count <= REACTION_SAME_PERSON_HALF:
                reaction_credit += count
            elif count <= REACTION_SAME_PERSON_CAP:
                reaction_credit += (
                    REACTION_SAME_PERSON_HALF
                    + (count - REACTION_SAME_PERSON_HALF) * 0.5
                )
            else:
                reaction_credit += (
                    REACTION_SAME_PERSON_HALF
                    + (REACTION_SAME_PERSON_CAP - REACTION_SAME_PERSON_HALF) * 0.5
                )

        reaction_rates[idx] = (
            (reaction_credit / n_active_days) if n_active_days > 0 else 0.0
        )

        # Reply ratio
        total_msgs = len(user_msgs)
        reply_ratios[idx] = (qualifying_replies / total_msgs) if total_msgs > 0 else 0.0

        # Also track reverse engagement (others engaging with this user)
        reverse_events = replies_to_user.get(uid, []) + reactions_to_user.get(uid, [])
        reverse_events.sort()

        # Merge and determine who engaged first per pair
        pair_first: dict[int, bool] = {}  # target -> True if user initiated
        all_engage = [(ts, target, True) for ts, target in engagement_events]
        all_engage += [(ts, from_uid, False) for ts, from_uid in reverse_events]
        all_engage.sort()

        for _ts, other, is_outbound in all_engage:
            if other not in pair_first:
                pair_first[other] = is_outbound

        initiated = sum(1 for v in pair_first.values() if v)
        total_pairs = len(pair_first)
        initiative_ratios[idx] = (initiated / total_pairs) if total_pairs > 0 else 0.5

        # -- Consistency & Recency --
        days_since = (now_ts - last_ts) / 86400 if last_ts > 0 else WINDOW_DAYS
        recency_values[idx] = math.exp(-DECAY_RATE * days_since)
        # Normalize consistency denominator to actual tenure for newer members
        _m = member_map.get(uid)
        _joined_ts = _m.joined_at.timestamp() if _m and _m.joined_at else 0.0
        _weeks_in_window = max(1, _window_days // 7)
        _tenure_wks = (
            max(1.0, (now_ts - _joined_ts) / (7 * 86400))
            if _joined_ts
            else float(_weeks_in_window)
        )
        consistency_values[idx] = n_active_weeks / min(
            float(_weeks_in_window), _tenure_wks
        )

        # -- Content Resonance --
        # "Posts" = messages with attachment OR conversation starters
        # (non-replies); tallied in the message pass above.
        if posts_count:
            resonance_values[idx] = posts_resonance / posts_count
        else:
            resonance_values[idx] = -1.0  # sentinel for "non-poster"

        # -- Posting Activity --
        capped_posts = sum(
            min(v, ATTACHMENT_DAILY_CAP) for v in daily_attachments.values()
        ) + sum(min(v, STARTER_DAILY_CAP) for v in daily_starters.values())
        posting_rates[idx] = (
            (capped_posts / n_active_days) if n_active_days > 0 else 0.0
        )

    # ------------------------------------------------------------------
    # Percentile ranking
    # ------------------------------------------------------------------

    reaction_rate_pctile = _percentile_ranks(reaction_rates)
    reply_ratio_pctile = _percentile_ranks(reply_ratios)
    posting_rate_pctile = _percentile_ranks(posting_rates)

    # Resonance: non-posters get neutral 0.5, posters get percentile ranked
    poster_indices = [i for i in range(n) if resonance_values[i] >= 0]
    poster_values = [resonance_values[i] for i in poster_indices]
    poster_pctile = _percentile_ranks(poster_values)
    resonance_pctile = [0.5] * n  # default neutral for non-posters
    for rank_idx, orig_idx in enumerate(poster_indices):
        resonance_pctile[orig_idx] = poster_pctile[rank_idx]

    # ------------------------------------------------------------------
    # Component scores and final score
    # ------------------------------------------------------------------

    scored_results: list[QualityScore] = []

    # Distinct active months per author over all history, for the tenure
    # buffer. One grouped query, fetched lazily the first time a member is
    # tenured enough to need it (instead of one query per member).
    months_active_by_user: dict[int, int] | None = None

    for idx, uid in enumerate(scored_ids):
        # Check minimum active days
        if active_days_arr[idx] < _min_active_days:
            results.append(
                QualityScore(
                    user_id=uid,
                    final_score=0,
                    engagement_given=0,
                    consistency_recency=0,
                    content_resonance=0,
                    posting_activity=0,
                    last_active_ts=last_active[idx],
                    status=STATUS_INSUFFICIENT,
                    tenure_buffer_days=0,
                    active_days=active_days_arr[idx],
                    active_weeks=active_weeks_arr[idx],
                )
            )
            continue

        # Engagement Given
        engagement_raw = (reaction_rate_pctile[idx] + reply_ratio_pctile[idx]) / 2.0
        initiative_mult = _initiative_multiplier(initiative_ratios[idx])
        engagement = min(engagement_raw * initiative_mult, 1.1)

        # Consistency & Recency
        consistency_recency = (
            recency_values[idx] * 0.60 + consistency_values[idx] * 0.40
        )

        # Content Resonance
        resonance = resonance_pctile[idx]

        # Posting Activity (floor 0.25 for non-posters)
        posting = (
            max(posting_rate_pctile[idx], 0.25)
            if posting_rates[idx] == 0
            else posting_rate_pctile[idx]
        )

        # Final weighted score
        final = (
            W_ENGAGEMENT * engagement
            + W_CONSISTENCY * consistency_recency
            + W_RESONANCE * resonance
            + W_POSTING * posting
        )

        # Tenure buffer
        mbr = member_map.get(uid)
        joined = mbr.joined_at if mbr else None
        buffer_days = 0
        if joined is not None and (now - joined).days >= TENURE_6MO_DAYS:
            if months_active_by_user is None:
                months_active_by_user = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT author_id,"
                        " COUNT(DISTINCT CAST(ts / 2592000 AS INTEGER))"
                        " FROM messages WHERE guild_id = ? GROUP BY author_id",
                        (guild_id,),
                    )
                }
            buffer_days = _tenure_buffer(
                joined, now, months_active_by_user.get(uid, 0)
            )

        scored_results.append(
            QualityScore(
                user_id=uid,
                final_score=round(final, 4),
                engagement_given=round(engagement, 4),
                consistency_recency=round(consistency_recency, 4),
                content_resonance=round(resonance, 4),
                posting_activity=round(posting, 4),
                last_active_ts=last_active[idx],
                status=STATUS_ACTIVE,
                tenure_buffer_days=buffer_days,
                active_days=active_days_arr[idx],
                active_weeks=active_weeks_arr[idx],
            )
        )

    # Sort scored by final_score descending
    scored_results.sort(key=lambda s: s.final_score, reverse=True)

    # Combine: scored first, then onboarding/leave/insufficient
    return scored_results + results


def build_quality_report(
    conn: sqlite3.Connection,
    guild_id: int,
    members: Sequence[discord.Member],
    now: datetime | None = None,
    window_days: int | None = None,
    min_active_days: int | None = None,
) -> dict:
    """Compute scores and shape them for the dashboard report.

    Shared by the web route and the hourly cache warmer so both store an
    identical payload under the same cache key. ``user_name`` is left blank —
    the route resolves display names per request.
    """
    scores = compute_quality_scores(
        conn,
        guild_id,
        members,
        now=now,
        window_days=window_days,
        min_active_days=min_active_days,
    )
    # Gender tags for the panel's per-gender totals (member_gender is the
    # mod-maintained roster; absent rows render as "unknown").
    genders = get_gender_map(conn, guild_id, [s.user_id for s in scores])

    entries = [
        {
            "user_id": str(s.user_id),
            "user_name": "",
            "final_score": s.final_score,
            "engagement_given": s.engagement_given,
            "consistency_recency": s.consistency_recency,
            "content_resonance": s.content_resonance,
            "posting_activity": s.posting_activity,
            "status": s.status,
            "active_days": s.active_days,
            "active_weeks": s.active_weeks,
            "gender": genders.get(s.user_id, "unknown"),
        }
        for s in scores
    ]
    scored = sum(1 for e in entries if e["status"] == STATUS_ACTIVE)
    return {"total_scored": scored, "entries": entries}
