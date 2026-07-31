"""DB layer for the no-contact list.

CRUD plus the one question every gate asks:
:func:`is_no_contact`. Pure decisions live in
``bot_modules/services/no_contact_logic.py`` — do not move logic here, and do
not move CRUD there.

**No in-memory cache, deliberately.** ``dm_perms`` caches its consent pairs at
boot, and that is safe because a stale cache there fails toward "not
connected". A stale no-contact cache fails the other way — it lets a blocked
member through until the next restart, which is precisely the failure this
feature cannot have. The gate paths (sending a whisper, running a pen-pal
match) are low-frequency, and the per-message alert path rides along with
writes the message store is already making, so a direct indexed read is
cheap enough to be the right trade.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from bot_modules.core.db_utils import open_db
from bot_modules.services.no_contact_logic import KIND_ATTEMPT, is_self_pair, pair_key


# ── Pairs ────────────────────────────────────────────────────────────────


def is_no_contact(db_path: Path, guild_id: int, user_a: int, user_b: int) -> bool:
    """Whether these two members are under a no-contact rule.

    Order-independent. This is the single question every gate asks; if it
    returns True the caller must refuse the contact AND present its ordinary
    success response — see the disclosure rules in docs/no_contact_spec.md.
    """
    with open_db(db_path) as conn:
        return is_no_contact_conn(conn, guild_id, user_a, user_b)


def is_no_contact_conn(
    conn: sqlite3.Connection, guild_id: int, user_a: int, user_b: int
) -> bool:
    """:func:`is_no_contact` for a caller that already holds a connection.

    Exists so features with their own DB session (Pen Pals' matching pass,
    Voice Master's permission build) can consult the list without opening a
    second connection — and, more importantly, without hand-rolling the query.
    Before this, ``no_contact_pairs`` was read by raw SQL from two unrelated
    modules, which made the table's shape their maintenance problem: adding an
    expiry or soft-delete column would have meant finding them by grep.
    """
    if is_self_pair(user_a, user_b):
        return False
    lo, hi = pair_key(user_a, user_b)
    row = conn.execute(
        "SELECT 1 FROM no_contact_pairs "
        "WHERE guild_id = ? AND user_low = ? AND user_high = ? LIMIT 1",
        (guild_id, lo, hi),
    ).fetchone()
    return row is not None


def no_contact_partners(db_path: Path, guild_id: int, user_id: int) -> set[int]:
    """Every member ``user_id`` is under a no-contact rule with.

    Used where a caller needs to filter a collection in one pass (the Guess
    Who candidate picker, the pen-pal match pool, the mention watcher) rather
    than asking :func:`is_no_contact` once per candidate.
    """
    with open_db(db_path) as conn:
        return no_contact_partners_conn(conn, guild_id, user_id)


def no_contact_partners_conn(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> set[int]:
    """:func:`no_contact_partners` for a caller that already holds a connection."""
    rows = conn.execute(
        "SELECT user_low, user_high FROM no_contact_pairs "
        "WHERE guild_id = ? AND (user_low = ? OR user_high = ?)",
        (guild_id, user_id, user_id),
    ).fetchall()
    out: set[int] = set()
    for row in rows:
        lo, hi = int(row["user_low"]), int(row["user_high"])
        out.add(hi if lo == user_id else lo)
    return out


def add_pair(
    db_path: Path,
    guild_id: int,
    user_a: int,
    user_b: int,
    *,
    created_by: int,
    protected_user_id: Optional[int] = None,
    reason: str = "",
) -> bool:
    """Create a no-contact entry. Returns False only for a self-pair.

    A duplicate add never rewrites ``reason`` or ``created_by`` — the other
    member must not be able to launder the record of why the entry is there.
    It can, however, escalate ``protected_user_id`` to NULL (mutual); see the
    inline comment for why leaving it alone is unsafe.

    Note this returns True for a duplicate as well as a fresh insert, and
    callers MUST NOT distinguish the two in what they show the member. "That
    entry already exists" would tell him she had already added one.
    """
    if is_self_pair(user_a, user_b):
        return False
    lo, hi = pair_key(user_a, user_b)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT protected_user_id FROM no_contact_pairs "
            "WHERE guild_id = ? AND user_low = ? AND user_high = ?",
            (guild_id, lo, hi),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO no_contact_pairs
                    (guild_id, user_low, user_high, protected_user_id,
                     created_by, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, lo, hi, protected_user_id, created_by, reason, time.time()),
            )
            return True
        # An entry already exists. Its reason and provenance are never
        # overwritten — otherwise the other party could launder them by
        # re-adding the pair himself.
        #
        # But the protected member CANNOT simply be left alone either. If he
        # adds the pair first, the row records him as protected; when she then
        # adds it, a plain no-op would leave him holding the only key to her
        # protection — free to lift it, while she can neither remove it nor
        # even see that it exists. That is precisely the abuse the removal
        # rule is meant to close, arriving through the add path.
        #
        # When the second party asks for the same separation, the entry
        # becomes MUTUAL: both of them wanted it, so neither gets to undo it
        # alone and only a moderator can lift it. Escalating to NULL is
        # one-way, so a pair can never be walked back down to single-party
        # control. A mod-set mutual entry is already NULL and stays there.
        existing = row["protected_user_id"]
        if existing is not None and protected_user_id != existing:
            conn.execute(
                "UPDATE no_contact_pairs SET protected_user_id = NULL "
                "WHERE guild_id = ? AND user_low = ? AND user_high = ?",
                (guild_id, lo, hi),
            )
    return True


def remove_pair(db_path: Path, guild_id: int, user_a: int, user_b: int) -> bool:
    """Delete a no-contact entry. Returns whether a row was actually removed.

    Authorisation is the caller's job — see ``no_contact_logic.can_remove``.
    """
    lo, hi = pair_key(user_a, user_b)
    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM no_contact_pairs "
            "WHERE guild_id = ? AND user_low = ? AND user_high = ?",
            (guild_id, lo, hi),
        )
    return (cur.rowcount or 0) > 0


def get_pair(
    db_path: Path, guild_id: int, user_a: int, user_b: int
) -> Optional[dict[str, Any]]:
    """Full record for one pair, or None."""
    lo, hi = pair_key(user_a, user_b)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT user_low, user_high, protected_user_id, created_by, "
            "reason, created_at FROM no_contact_pairs "
            "WHERE guild_id = ? AND user_low = ? AND user_high = ?",
            (guild_id, lo, hi),
        ).fetchone()
    return _pair_row(row) if row else None


def list_pairs(db_path: Path, guild_id: int) -> list[dict[str, Any]]:
    """Every no-contact entry in a guild, newest first (the mod panel's list)."""
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT user_low, user_high, protected_user_id, created_by, "
            "reason, created_at FROM no_contact_pairs "
            "WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        ).fetchall()
    return [_pair_row(r) for r in rows]


def list_pairs_for_user(
    db_path: Path, guild_id: int, user_id: int
) -> list[dict[str, Any]]:
    """Entries involving one member — what the self-service command shows them."""
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT user_low, user_high, protected_user_id, created_by, "
            "reason, created_at FROM no_contact_pairs "
            "WHERE guild_id = ? AND (user_low = ? OR user_high = ?) "
            "ORDER BY created_at DESC",
            (guild_id, user_id, user_id),
        ).fetchall()
    return [_pair_row(r) for r in rows]


def _pair_row(row: Any) -> dict[str, Any]:
    protected = row["protected_user_id"]
    return {
        "user_low": int(row["user_low"]),
        "user_high": int(row["user_high"]),
        "protected_user_id": int(protected) if protected is not None else None,
        "created_by": int(row["created_by"]),
        "reason": row["reason"] or "",
        "created_at": float(row["created_at"] or 0),
    }


# ── Alert settings ───────────────────────────────────────────────────────


def get_settings(db_path: Path, guild_id: int) -> dict[str, int]:
    """Alert destination for a guild. Missing row reads as "unconfigured".

    A guild without a configured channel still gets full enforcement — only
    the alerting is optional. That ordering matters: the feature must never
    depend on someone having finished setting it up.
    """
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT alert_channel_id, alert_role_id FROM no_contact_settings "
            "WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if not row:
        return {"alert_channel_id": 0, "alert_role_id": 0}
    return {
        "alert_channel_id": int(row["alert_channel_id"] or 0),
        "alert_role_id": int(row["alert_role_id"] or 0),
    }


def set_settings(
    db_path: Path,
    guild_id: int,
    *,
    alert_channel_id: int,
    alert_role_id: int,
) -> None:
    """Write the alert destination (dashboard-only; this is server config)."""
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO no_contact_settings (guild_id, alert_channel_id, alert_role_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                alert_channel_id = excluded.alert_channel_id,
                alert_role_id = excluded.alert_role_id
            """,
            (guild_id, alert_channel_id, alert_role_id),
        )


# ── Events (mod-visible only) ────────────────────────────────────────────


def record_event(
    db_path: Path,
    guild_id: int,
    *,
    actor_id: int,
    target_id: int,
    kind: str,
    surface: str = "",
    channel_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> None:
    """Record a blocked attempt or a mention/reply alert.

    Staff-facing only. The protected member is never notified from here: the
    sender already sees a fake success and learns nothing, and a notification
    on every attempt would give him an indirect way to distress her.
    """
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO no_contact_events
                (guild_id, actor_id, target_id, kind, surface,
                 channel_id, message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                actor_id,
                target_id,
                kind,
                surface,
                channel_id,
                message_id,
                time.time(),
            ),
        )


def check_and_record(
    db_path: Path,
    guild_id: int,
    *,
    actor_id: int,
    target_id: int,
    surface: str,
) -> bool:
    """Return True if contact is blocked, recording the attempt when it is.

    The one call a gate needs. Combining the check with the log keeps the two
    from drifting apart — an enforcement path that forgot to record would
    leave staff with no evidence that someone kept trying, which is the whole
    value of the log.

    Callers run this in a thread (``asyncio.to_thread``) and, when it returns
    True, must present their ORDINARY SUCCESS response without performing the
    action.
    """
    if not is_no_contact(db_path, guild_id, actor_id, target_id):
        return False
    record_event(
        db_path,
        guild_id,
        actor_id=actor_id,
        target_id=target_id,
        kind=KIND_ATTEMPT,
        surface=surface,
    )
    return True


def list_events(
    db_path: Path, guild_id: int, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Recent events, newest first, for the mod dashboard panel."""
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, actor_id, target_id, kind, surface, channel_id, "
            "message_id, created_at FROM no_contact_events "
            "WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, int(limit)),
        ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "actor_id": int(r["actor_id"]),
            "target_id": int(r["target_id"]),
            "kind": r["kind"],
            "surface": r["surface"] or "",
            "channel_id": int(r["channel_id"]) if r["channel_id"] else None,
            "message_id": int(r["message_id"]) if r["message_id"] else None,
            "created_at": float(r["created_at"] or 0),
        }
        for r in rows
    ]
