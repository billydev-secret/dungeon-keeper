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


# The dashboard sends the exact ids the panel is showing — never "everything
# matching the current filter", which is a far more dangerous primitive for a
# guild-scoped moderation queue. This bounds how many one call can touch; the
# panel's page size is capped at the same number (see /rules-watch/events).
MAX_BULK_LABEL = 200


def bulk_upsert_labels(
    conn: sqlite3.Connection,
    guild_id: int,
    event_ids: list[int],
    *,
    is_violation: bool,
    corrected_rule: str | None = None,
    labeled_by: int | None = None,
    notes: str | None = None,
) -> dict[str, list[int]]:
    """Apply one label to many events at once, in the caller's transaction.

    Same upsert semantics as :func:`upsert_label` — a second label on an
    already-labelled event overwrites it, id for id — applied to every id in
    ``event_ids`` that both exists and belongs to ``guild_id``. An id that
    fails either check is skipped, never labelled, and reported back rather
    than silently dropped.

    This is the guild scoping the caller MUST get from here: unlike
    :func:`get_event`, which looks an event up by id alone with no guild
    check, this function never writes a row outside ``guild_id`` — an id
    from another guild lands in ``skipped``, indistinguishable from an id
    that plain doesn't exist.

    Raises ``ValueError`` if ``event_ids`` has more than
    :data:`MAX_BULK_LABEL` entries (callers behind the HTTP route never hit
    this — the request body is capped at the same number before it reaches
    here — but it is enforced here too so the invariant holds for any other
    caller).

    Returns ``{"labeled": [...], "skipped": [...]}``: the ids actually
    labelled, and the ids not found in this guild, each in the order
    ``event_ids`` was given (duplicates in the input collapse to one entry).
    """
    if len(event_ids) > MAX_BULK_LABEL:
        raise ValueError(
            f"cannot label more than {MAX_BULK_LABEL} events in one call "
            f"(got {len(event_ids)})"
        )

    if not event_ids:
        return {"labeled": [], "skipped": []}

    seen: set[int] = set()
    ordered_ids: list[int] = []
    for eid in event_ids:
        if eid not in seen:
            seen.add(eid)
            ordered_ids.append(eid)

    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"SELECT id FROM rules_events WHERE guild_id = ? AND id IN ({placeholders})",
        (guild_id, *ordered_ids),
    ).fetchall()
    valid_ids = {row["id"] for row in rows}

    labeled: list[int] = []
    skipped: list[int] = []
    for eid in ordered_ids:
        if eid in valid_ids:
            upsert_label(
                conn,
                eid,
                is_violation=is_violation,
                corrected_rule=corrected_rule,
                labeled_by=labeled_by,
                notes=notes,
            )
            labeled.append(eid)
        else:
            skipped.append(eid)

    return {"labeled": labeled, "skipped": skipped}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_event(
    conn: sqlite3.Connection, event_id: int, guild_id: int
) -> sqlite3.Row | None:
    """One event, scoped to the guild the caller is actually viewing.

    ``guild_id`` is required rather than optional on purpose. This function
    used to select on id alone, and both of its callers -- the detail route
    and the label route -- are reached with an event id straight from the URL,
    so a moderator of one guild could read another guild's event detail, and
    write a label onto it, by knowing nothing but the number. Making the scope
    a required argument means a future caller cannot forget it; the failure
    mode is a TypeError at import time rather than a silent cross-guild read.
    """
    return conn.execute(
        "SELECT * FROM rules_events WHERE id = ? AND guild_id = ?",
        (event_id, guild_id),
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
