"""To do list — DB helpers extracted from todo_cog for testability."""

from __future__ import annotations

import sqlite3
import time


def create_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    added_by: int,
    task: str,
) -> int:
    """Insert a new to do and return its ID."""
    cur = conn.execute(
        "INSERT INTO todos (guild_id, added_by, task, created_at) VALUES (?, ?, ?, ?)",
        (guild_id, added_by, task, time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_todos(
    conn: sqlite3.Connection,
    guild_id: int,
    include_completed: bool = False,
) -> list[dict]:
    """Return todos for a guild, newest first."""
    if include_completed:
        sql = "SELECT * FROM todos WHERE guild_id = ? ORDER BY created_at DESC"
    else:
        sql = "SELECT * FROM todos WHERE guild_id = ? AND completed_at IS NULL ORDER BY created_at DESC"
    rows = conn.execute(sql, (guild_id,)).fetchall()
    return [dict(r) for r in rows]


def complete_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    todo_id: int,
) -> bool:
    """Mark a to do complete. Returns True if a row was updated."""
    cur = conn.execute(
        "UPDATE todos SET completed_at = ? WHERE id = ? AND guild_id = ? AND completed_at IS NULL",
        (time.time(), todo_id, guild_id),
    )
    return (cur.rowcount or 0) > 0


def delete_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    todo_id: int,
) -> bool:
    """Delete a to do by ID. Returns True if deleted."""
    cur = conn.execute(
        "DELETE FROM todos WHERE id = ? AND guild_id = ?",
        (todo_id, guild_id),
    )
    return (cur.rowcount or 0) > 0
