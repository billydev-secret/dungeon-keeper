"""Database access for hidden-channel holds (``hidden_channels`` table).

Thin synchronous helpers over the table from migration
``058_hidden_channels.sql``. Cog callers wrap these in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from bot_modules.hidden_channels.overwrites import OverwriteRecord

HiddenRow = dict[str, Any]


def create_hidden(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_id: int,
    original_parent_id: int | None,
    original_position: int,
    stored_overwrites: list[OverwriteRecord],
    hidden_by: int,
) -> int:
    """Insert an active hidden-channel row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO hidden_channels
            (guild_id, channel_id, original_parent_id, original_position,
             stored_overwrites, hidden_by, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            guild_id,
            channel_id,
            original_parent_id,
            original_position,
            json.dumps(stored_overwrites),
            hidden_by,
            time.time(),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def delete_hidden(conn: sqlite3.Connection, hidden_id: int) -> bool:
    """Delete a hold outright. Returns False if the row was already gone.

    Used to roll back the row written ahead of a ``channel.edit()`` that then
    failed — the channel is untouched, so the snapshot must not linger.
    """
    cur = conn.execute("DELETE FROM hidden_channels WHERE id = ?", (hidden_id,))
    return cur.rowcount > 0


def get_active_hidden(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> HiddenRow | None:
    """Return the active hold for a channel, or ``None``."""
    row = conn.execute(
        "SELECT * FROM hidden_channels "
        "WHERE guild_id = ? AND channel_id = ? AND status = 'active'",
        (guild_id, channel_id),
    ).fetchone()
    return dict(row) if row else None


def list_active_hidden(conn: sqlite3.Connection, guild_id: int) -> list[HiddenRow]:
    """Return all active holds in a guild, oldest first."""
    rows = conn.execute(
        "SELECT * FROM hidden_channels "
        "WHERE guild_id = ? AND status = 'active' ORDER BY created_at",
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_restored(conn: sqlite3.Connection, hidden_id: int) -> bool:
    """Mark a hold restored. Returns False if it wasn't active."""
    cur = conn.execute(
        "UPDATE hidden_channels SET status = 'restored', restored_at = ? "
        "WHERE id = ? AND status = 'active'",
        (time.time(), hidden_id),
    )
    return cur.rowcount > 0
