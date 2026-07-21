"""Database access for inactive-channel holds (``inactive_members`` table).

Thin, synchronous helpers over the table created by migration
``057_inactive_members.sql``. Callers wrap these in ``asyncio.to_thread`` the
same way the jail flow does with ``services.moderation``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

InactiveRow = dict[str, Any]


def create_inactive(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str,
    stored_roles: list[int],
    source: str,
) -> int:
    """Insert an active inactive-hold row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO inactive_members
            (guild_id, user_id, moderator_id, reason, stored_roles, source,
             created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            guild_id,
            user_id,
            moderator_id,
            reason,
            json.dumps(stored_roles),
            source,
            time.time(),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_active_inactive(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> InactiveRow | None:
    """Return the active inactive-hold for a member, or ``None``."""
    row = conn.execute(
        "SELECT * FROM inactive_members "
        "WHERE guild_id = ? AND user_id = ? AND status = 'active'",
        (guild_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def active_inactive_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    """Return the set of user IDs currently held inactive in this guild."""
    rows = conn.execute(
        "SELECT user_id FROM inactive_members "
        "WHERE guild_id = ? AND status = 'active'",
        (guild_id,),
    ).fetchall()
    return {r["user_id"] for r in rows}


def reactivate_inactive(
    conn: sqlite3.Connection, inactive_id: int, *, reason: str
) -> bool:
    """Mark an inactive-hold reactivated. Returns False if it wasn't active."""
    cur = conn.execute(
        "UPDATE inactive_members "
        "SET status = 'reactivated', reactivated_at = ?, reactivate_reason = ? "
        "WHERE id = ? AND status = 'active'",
        (time.time(), reason, inactive_id),
    )
    return cur.rowcount > 0
