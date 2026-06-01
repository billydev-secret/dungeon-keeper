"""To do list — DB helpers extracted from todo_cog for testability."""

from __future__ import annotations

import sqlite3
import time

TASK_MAX_LEN = 500


def create_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    added_by: int,
    task: str,
    *,
    description: str | None = None,
    source_message_url: str | None = None,
) -> int:
    """Insert a new to do and return its ID."""
    cur = conn.execute(
        "INSERT INTO todos (guild_id, added_by, task, description, source_message_url, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, added_by, task, description, source_message_url, time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]
