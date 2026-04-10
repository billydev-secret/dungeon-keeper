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
    (0.60, 1.10),   # 60%+ initiated
    (0.40, 1.00),   # 40-60%
    (0.20, 0.92),   # 20-40%
    (0.00, 0.85),   # under 20%
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


def add_leave(conn: sqlite3.Connection, guild_id: int, user_id: int,
              start_ts: float, end_ts: float) -> None:
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


def get_leaves(conn: sqlite3.Connection, guild_id: int) -> dict[int, tuple[float, float]]:
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


def _initiative_multiplier(initiated_ratio: float) -> float:
    for threshold, mult in INITIATIVE_TIERS:
        if initiated_ratio >= threshold:
            return mult
    return 0.85


def _tenure_buffer(joined_at: datetime | None, now: datetime,
                   conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    """Compute tenure buffer days based on join date and historical activity."""
    if joined_at is None:
        return 0
    tenure_days = (now - joined_at).days
    if tenure_days < TENURE_6MO_DAYS:
        return 0

    # Check "consistent historical activity": at least 1 active week per month
    # for the majority of tenure.
    tenure_months = max(1, tenure_days // 30)
    active_months = conn.execute(
        """
        SELECT COUNT(DISTINCT CAST(ts / 2592000 AS INTEGER)) AS months
        FROM messages
        WHERE guild_id = ? AND author_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    months_active = int(active_months["months"]) if active_months else 0

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
    _min_active_days = min_active_days if min_active_days is not None else MIN_ACTIVE_DAYS
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
                results.append(QualityScore(
                    user_id=m.id, final_score=0, engagement_given=0,
                    consistency_recency=0, content_resonance=0, posting_activity=0,
                    last_active_ts=0, status=STATUS_LEAVE, tenure_buffer_days=0,
                    active_days=0, active_weeks=0,
                ))
                continue

        # Onboarding
        if m.joined_at and (now - m.joined_at).days < ONBOARDING_DAYS:
            results.append(QualityScore(
                user_id=m.id, final_score=0, engagement_given=0,
                consistency_recency=0, content_resonance=0, posting_activity=0,
                last_active_ts=0, status=STATUS_ONBOARDING, tenure_buffer_days=0,
                active_days=0, active_weeks=0,
            ))
            continue

        scored_ids.append(m.id)

    if not scored_ids:
        return results

    # ------------------------------------------------------------------
    # Bulk data fetch
    # ------------------------------------------------------------------
    id_set = set(scored_ids)

    # Messages in window
    msg_rows = conn.execute(
        "SELECT message_id, author_id, channel_id, ts, content, reply_to_id "
        "FROM messages WHERE guild_id = ? AND ts >= ? ORDER BY ts",
        (guild_id, int(window_start)),
    ).fetchall()

    # Attachment message IDs in window
    attach_rows = conn.execute(
        """
        SELECT DISTINCT ma.message_id
        FROM message_attachments ma
        JOIN messages m ON ma.message_id = m.message_id
        WHERE m.guild_id = ? AND m.ts >= ?
        """,
        (guild_id, int(window_start)),
    ).fetchall()
    attachment_msg_ids = {int(r["message_id"]) for r in attach_rows}

    # Reaction log (individual reactions given)
    react_rows = conn.execute(
        "SELECT reactor_id, author_id, ts FROM reaction_log "
        "WHERE guild_id = ? AND ts >= ?",
        (guild_id, int(window_start)),
    ).fetchall()

    # Reaction counts received (aggregate per message)
    reaction_count_rows = conn.execute(
        """
        SELECT mr.message_id, SUM(mr.count) AS total
        FROM message_reactions mr
        JOIN messages m ON mr.message_id = m.message_id
        WHERE m.guild_id = ? AND m.ts >= ?
        GROUP BY mr.message_id
        """,
        (guild_id, int(window_start)),
    ).fetchall()
    reaction_counts: dict[int, int] = {
        int(r["message_id"]): int(r["total"]) for r in reaction_count_rows
    }

    # ------------------------------------------------------------------
    # Per-member aggregation
    # ------------------------------------------------------------------

    # Message-level data
    msgs_by_author: dict[int, list[dict]] = defaultdict(list)
    msg_author_map: dict[int, int] = {}  # message_id -> author_id
    for row in msg_rows:
        uid = int(row["author_id"])
        msg_author_map[int(row["message_id"])] = uid
        if uid in id_set:
            msgs_by_author[uid].append({
                "message_id": int(row["message_id"]),
                "ts": int(row["ts"]),
                "content": row["content"],
                "reply_to_id": row["reply_to_id"],
                "channel_id": int(row["channel_id"]),
            })

    # Reaction log data
    reactions_given: dict[int, list[tuple[int, int]]] = defaultdict(list)  # reactor -> [(author, ts)]
    for row in react_rows:
        rid = int(row["reactor_id"])
        if rid in id_set:
            reactions_given[rid].append((int(row["author_id"]), int(row["ts"])))

    # Replies received per author (from all messages in window)
    replies_received: dict[int, int] = defaultdict(int)
    for row in msg_rows:
        reply_to = row["reply_to_id"]
        if reply_to is not None:
            target_uid = msg_author_map.get(int(reply_to))
            if target_uid is not None and target_uid in id_set:
                replies_received[target_uid] += 1

    # Replies per message (for content resonance per-post lookup)
    replies_per_msg: dict[int, int] = defaultdict(int)
    for row in msg_rows:
        if row["reply_to_id"] is not None:
            replies_per_msg[int(row["reply_to_id"])] += 1

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

    # Pre-build reverse engagement indices (others engaging with each user)
    replies_to_user: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in msg_rows:
        if row["reply_to_id"] is not None:
            _rev_target = msg_author_map.get(int(row["reply_to_id"]))
            if _rev_target is not None:
                _from_uid = int(row["author_id"])
                if _from_uid != _rev_target:
                    replies_to_user[_rev_target].append((int(row["ts"]), _from_uid))

    reactions_to_user: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for rr in react_rows:
        _rr_author = int(rr["author_id"])
        _rr_reactor = int(rr["reactor_id"])
        if _rr_reactor != _rr_author:
            reactions_to_user[_rr_author].append((int(rr["ts"]), _rr_reactor))

    for idx, uid in enumerate(scored_ids):
        user_msgs = msgs_by_author.get(uid, [])
        user_reactions = reactions_given.get(uid, [])

        # Active days and weeks
        activity_days: set[int] = set()
        activity_weeks: set[int] = set()
        all_ts: list[int] = []

        for msg in user_msgs:
            ts = msg["ts"]
            all_ts.append(ts)
            activity_days.add(_day_key(ts))
            activity_weeks.add(_week_key(ts))

        for _target, ts in user_reactions:
            all_ts.append(ts)
            activity_days.add(_day_key(ts))
            activity_weeks.add(_week_key(ts))

        n_active_days = len(activity_days)
        n_active_weeks = len(activity_weeks)
        active_days_arr[idx] = n_active_days
        active_weeks_arr[idx] = n_active_weeks

        # Last active
        last_ts = max(all_ts) if all_ts else 0.0
        last_active[idx] = last_ts

        # -- Engagement Given --

        # Reaction rate (per active day), with anti-gaming
        reaction_credit = 0.0
        daily_reaction_targets: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for target, ts in user_reactions:
            day = _day_key(ts)
            daily_reaction_targets[day][target] += 1

        for day_targets in daily_reaction_targets.values():
            for _target, count in day_targets.items():
                if count <= REACTION_SAME_PERSON_HALF:
                    reaction_credit += count
                elif count <= REACTION_SAME_PERSON_CAP:
                    reaction_credit += REACTION_SAME_PERSON_HALF + (count - REACTION_SAME_PERSON_HALF) * 0.5
                else:
                    reaction_credit += REACTION_SAME_PERSON_HALF + (REACTION_SAME_PERSON_CAP - REACTION_SAME_PERSON_HALF) * 0.5

        reaction_rates[idx] = (reaction_credit / n_active_days) if n_active_days > 0 else 0.0

        # Reply ratio
        total_msgs = len(user_msgs)
        qualifying_replies = sum(
            1 for msg in user_msgs
            if msg["reply_to_id"] is not None
            and msg["content"] is not None
            and len(msg["content"]) >= MIN_REPLY_CHARS
        )
        reply_ratios[idx] = (qualifying_replies / total_msgs) if total_msgs > 0 else 0.0

        # Initiative ratio

        # Build chronological engagement events for this user
        engagement_events: list[tuple[int, int]] = []  # (ts, target_uid)
        for msg in user_msgs:
            if msg["reply_to_id"] is not None:
                reply_target = msg_author_map.get(int(msg["reply_to_id"]))
                if reply_target is not None and reply_target != uid:
                    engagement_events.append((msg["ts"], reply_target))
        for react_target, react_ts in user_reactions:
            if react_target != uid:
                engagement_events.append((react_ts, react_target))
        engagement_events.sort()

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
        _tenure_wks = max(1.0, (now_ts - _joined_ts) / (7 * 86400)) if _joined_ts else float(_weeks_in_window)
        consistency_values[idx] = n_active_weeks / min(float(_weeks_in_window), _tenure_wks)

        # -- Content Resonance --
        # "Posts" = messages with attachment OR conversation starters (non-replies)
        user_posts: list[int] = []  # message_ids that qualify as posts
        for msg in user_msgs:
            is_attachment = msg["message_id"] in attachment_msg_ids
            is_starter = msg["reply_to_id"] is None
            if is_attachment or is_starter:
                user_posts.append(msg["message_id"])

        if user_posts:
            total_resonance = 0.0
            for mid in user_posts:
                total_resonance += reaction_counts.get(mid, 0)
                total_resonance += replies_per_msg.get(mid, 0)
            resonance_values[idx] = total_resonance / len(user_posts)
        else:
            resonance_values[idx] = -1.0  # sentinel for "non-poster"

        # -- Posting Activity --
        daily_attachments: dict[int, int] = defaultdict(int)
        daily_starters: dict[int, int] = defaultdict(int)
        for msg in user_msgs:
            day = _day_key(msg["ts"])
            if msg["message_id"] in attachment_msg_ids:
                daily_attachments[day] += 1
            if msg["reply_to_id"] is None:
                daily_starters[day] += 1

        capped_posts = sum(
            min(v, ATTACHMENT_DAILY_CAP) for v in daily_attachments.values()
        ) + sum(
            min(v, STARTER_DAILY_CAP) for v in daily_starters.values()
        )
        posting_rates[idx] = (capped_posts / n_active_days) if n_active_days > 0 else 0.0

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

    for idx, uid in enumerate(scored_ids):
        # Check minimum active days
        if active_days_arr[idx] < _min_active_days:
            results.append(QualityScore(
                user_id=uid, final_score=0, engagement_given=0,
                consistency_recency=0, content_resonance=0, posting_activity=0,
                last_active_ts=last_active[idx], status=STATUS_INSUFFICIENT,
                tenure_buffer_days=0, active_days=active_days_arr[idx],
                active_weeks=active_weeks_arr[idx],
            ))
            continue

        # Engagement Given
        engagement_raw = (reaction_rate_pctile[idx] + reply_ratio_pctile[idx]) / 2.0
        initiative_mult = _initiative_multiplier(initiative_ratios[idx])
        engagement = min(engagement_raw * initiative_mult, 1.1)

        # Consistency & Recency
        consistency_recency = recency_values[idx] * 0.60 + consistency_values[idx] * 0.40

        # Content Resonance
        resonance = resonance_pctile[idx]

        # Posting Activity (floor 0.25 for non-posters)
        posting = max(posting_rate_pctile[idx], 0.25) if posting_rates[idx] == 0 else posting_rate_pctile[idx]

        # Final weighted score
        final = (
            W_ENGAGEMENT * engagement
            + W_CONSISTENCY * consistency_recency
            + W_RESONANCE * resonance
            + W_POSTING * posting
        )

        # Tenure buffer
        mbr = member_map.get(uid)
        buffer_days = _tenure_buffer(
            mbr.joined_at if mbr else None, now, conn, guild_id, uid,
        )

        scored_results.append(QualityScore(
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
        ))

    # Sort scored by final_score descending
    scored_results.sort(key=lambda s: s.final_score, reverse=True)

    # Combine: scored first, then onboarding/leave/insufficient
    return scored_results + results
