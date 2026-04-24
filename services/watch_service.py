"""Watch list — DB helpers extracted from watch_cog/watch_commands for testability."""

from __future__ import annotations

import sqlite3


def add_watched_user(
    conn: sqlite3.Connection,
    guild_id: int,
    watched_user_id: int,
    watcher_user_id: int,
) -> bool:
    """Add a watch entry. Returns True if a new row was inserted."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO watched_users (guild_id, watched_user_id, watcher_user_id)
        VALUES (?, ?, ?)
        """,
        (guild_id, watched_user_id, watcher_user_id),
    )
    return (cur.rowcount or 0) > 0


def remove_watched_user(
    conn: sqlite3.Connection,
    guild_id: int,
    watched_user_id: int,
    watcher_user_id: int,
) -> bool:
    """Remove a watch entry. Returns True if a row was deleted."""
    cur = conn.execute(
        "DELETE FROM watched_users WHERE guild_id = ? AND watched_user_id = ? AND watcher_user_id = ?",
        (guild_id, watched_user_id, watcher_user_id),
    )
    return (cur.rowcount or 0) > 0


def load_watched_users(conn: sqlite3.Connection, guild_id: int) -> dict[int, set[int]]:
    """Return {watched_user_id: {watcher_user_id, ...}} for the given guild."""
    rows = conn.execute(
        "SELECT watched_user_id, watcher_user_id FROM watched_users WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    result: dict[int, set[int]] = {}
    for row in rows:
        result.setdefault(row["watched_user_id"], set()).add(row["watcher_user_id"])
    return result
