"""Rules Watch — DB layer.

Handles storage and retrieval of moderation events and human labels.
All functions accept an open sqlite3.Connection; callers own the transaction.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


# ---------------------------------------------------------------------------
# Event insertion
# ---------------------------------------------------------------------------

def insert_event(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    message_id: int,
    author_id: int,
    channel_id: int,
    detected_at: float | None = None,
    target_id: int | None = None,
    target_confidence: str | None = None,
    window_json: str | None = None,
    # Content signals
    guard_verdict: str | None = None,
    guard_rule: str | None = None,
    guard_reason: str | None = None,
    guard_confidence: float | None = None,
    slur_signal: int = 0,
    vader_compound: float | None = None,
    vader_trajectory: float | None = None,
    # Context signals
    mutual_interaction_count: int | None = None,
    reciprocity_ratio: float | None = None,
    consent_pair_active: int = 0,
    consent_pair_recently_revoked: int = 0,
    dm_tier_mismatch: int = 0,
    thread_reciprocity_ratio: float | None = None,
    persistence_count: int = 0,
    boundary_token_crossed: int = 0,
    target_withdrew: int = 0,
    tenure_days: int | None = None,
    # Scoring
    priority_score: float | None = None,
    priority_tier: str | None = None,
    priority_reason: str | None = None,
) -> int:
    """Insert a new rules event and return its id."""
    cur = conn.execute(
        """
        INSERT INTO rules_events (
            guild_id, message_id, author_id, channel_id, detected_at,
            target_id, target_confidence, window_json,
            guard_verdict, guard_rule, guard_reason, guard_confidence,
            slur_signal, vader_compound, vader_trajectory,
            mutual_interaction_count, reciprocity_ratio,
            consent_pair_active, consent_pair_recently_revoked, dm_tier_mismatch,
            thread_reciprocity_ratio, persistence_count, boundary_token_crossed,
            target_withdrew, tenure_days,
            priority_score, priority_tier, priority_reason
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?
        )
        """,
        (
            guild_id, message_id, author_id, channel_id,
            detected_at if detected_at is not None else time.time(),
            target_id, target_confidence, window_json,
            guard_verdict, guard_rule, guard_reason, guard_confidence,
            slur_signal, vader_compound, vader_trajectory,
            mutual_interaction_count, reciprocity_ratio,
            consent_pair_active, consent_pair_recently_revoked, dm_tier_mismatch,
            thread_reciprocity_ratio, persistence_count, boundary_token_crossed,
            target_withdrew, tenure_days,
            priority_score, priority_tier, priority_reason,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def update_alert_message_id(
    conn: sqlite3.Connection, event_id: int, alert_message_id: int
) -> None:
    conn.execute(
        "UPDATE rules_events SET alert_message_id = ? WHERE id = ?",
        (alert_message_id, event_id),
    )


def update_withdrawal_flag(
    conn: sqlite3.Connection,
    event_id: int,
    withdrew: bool,
    *,
    new_priority_score: float | None = None,
    new_priority_tier: str | None = None,
    new_priority_reason: str | None = None,
) -> None:
    """Mark whether the target went silent after the event was recorded.

    Optionally update the priority fields if the tier escalated.
    """
    if new_priority_score is not None:
        conn.execute(
            """
            UPDATE rules_events
            SET target_withdrew = ?,
                priority_score = ?,
                priority_tier = ?,
                priority_reason = ?
            WHERE id = ?
            """,
            (int(withdrew), new_priority_score, new_priority_tier,
             new_priority_reason, event_id),
        )
    else:
        conn.execute(
            "UPDATE rules_events SET target_withdrew = ? WHERE id = ?",
            (int(withdrew), event_id),
        )


# ---------------------------------------------------------------------------
# Label capture
# ---------------------------------------------------------------------------

def upsert_label(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    is_violation: bool,
    corrected_rule: str | None = None,
    labeled_by: int | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO rules_labels (event_id, is_violation, corrected_rule, labeled_by, labeled_at, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            is_violation   = excluded.is_violation,
            corrected_rule = excluded.corrected_rule,
            labeled_by     = excluded.labeled_by,
            labeled_at     = excluded.labeled_at,
            notes          = excluded.notes
        """,
        (event_id, int(is_violation), corrected_rule, labeled_by, time.time(), notes),
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rules_events WHERE id = ?", (event_id,)
    ).fetchone()


def get_pending_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Return events that have not yet been labeled, newest first."""
    if tier:
        return conn.execute(
            """
            SELECT e.* FROM rules_events e
            LEFT JOIN rules_labels l ON l.event_id = e.id
            WHERE e.guild_id = ? AND e.priority_tier = ? AND l.event_id IS NULL
            ORDER BY e.detected_at DESC
            LIMIT ? OFFSET ?
            """,
            (guild_id, tier, limit, offset),
        ).fetchall()
    return conn.execute(
        """
        SELECT e.* FROM rules_events e
        LEFT JOIN rules_labels l ON l.event_id = e.id
        WHERE e.guild_id = ? AND l.event_id IS NULL
        ORDER BY e.priority_score DESC, e.detected_at DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset),
    ).fetchall()


def get_all_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Return all events (labeled and unlabeled), newest first."""
    if tier:
        return conn.execute(
            """
            SELECT e.*, l.is_violation, l.corrected_rule, l.labeled_by, l.labeled_at
            FROM rules_events e
            LEFT JOIN rules_labels l ON l.event_id = e.id
            WHERE e.guild_id = ? AND e.priority_tier = ?
            ORDER BY e.detected_at DESC
            LIMIT ? OFFSET ?
            """,
            (guild_id, tier, limit, offset),
        ).fetchall()
    return conn.execute(
        """
        SELECT e.*, l.is_violation, l.corrected_rule, l.labeled_by, l.labeled_at
        FROM rules_events e
        LEFT JOIN rules_labels l ON l.event_id = e.id
        WHERE e.guild_id = ?
        ORDER BY e.detected_at DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset),
    ).fetchall()


def get_stats(conn: sqlite3.Connection, guild_id: int) -> dict[str, Any]:
    """Return aggregate stats for the dashboard."""
    total = conn.execute(
        "SELECT COUNT(*) FROM rules_events WHERE guild_id = ?", (guild_id,)
    ).fetchone()[0]

    labeled = conn.execute(
        """
        SELECT COUNT(*) FROM rules_labels l
        JOIN rules_events e ON e.id = l.event_id
        WHERE e.guild_id = ?
        """,
        (guild_id,),
    ).fetchone()[0]

    confirmed = conn.execute(
        """
        SELECT COUNT(*) FROM rules_labels l
        JOIN rules_events e ON e.id = l.event_id
        WHERE e.guild_id = ? AND l.is_violation = 1
        """,
        (guild_id,),
    ).fetchone()[0]

    by_tier = {
        row["priority_tier"]: row["cnt"]
        for row in conn.execute(
            """
            SELECT priority_tier, COUNT(*) as cnt
            FROM rules_events WHERE guild_id = ?
            GROUP BY priority_tier
            """,
            (guild_id,),
        ).fetchall()
    }

    by_rule = {
        row["guard_rule"]: row["cnt"]
        for row in conn.execute(
            """
            SELECT guard_rule, COUNT(*) as cnt
            FROM rules_events
            WHERE guild_id = ? AND guard_rule IS NOT NULL
            GROUP BY guard_rule
            ORDER BY cnt DESC
            """,
            (guild_id,),
        ).fetchall()
    }

    return {
        "total": total,
        "labeled": labeled,
        "confirmed": confirmed,
        "false_positives": labeled - confirmed,
        "fp_rate": round((labeled - confirmed) / labeled, 3) if labeled else None,
        "by_tier": by_tier,
        "by_rule": by_rule,
    }


# ---------------------------------------------------------------------------
# Tenure helper
# ---------------------------------------------------------------------------

def compute_tenure_days(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> int | None:
    """Return days since the user's first recorded member_event in this guild."""
    row = conn.execute(
        "SELECT MIN(ts) as first_ts FROM member_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None or row["first_ts"] is None:
        # Fall back to first stored message
        row = conn.execute(
            "SELECT MIN(ts) as first_ts FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchone()
    if row is None or row["first_ts"] is None:
        return None
    return int((time.time() - float(row["first_ts"])) / 86400)
