"""To do list — DB helpers extracted from todo_cog for testability.

Covers the task rows themselves plus the sticky board's placement. Recurring
task definitions live in ``todo_recurring_service``.

Every function takes ``now_ts`` explicitly where it needs the clock, so
time-dependent behavior stays deterministic in tests.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

TASK_MAX_LEN = 500

#: Ceiling on a single list query — the dashboard paginates visually, not by
#: query, and the board renders far fewer than this.
LIST_LIMIT = 200

_TODO_COLS = (
    "id, guild_id, added_by, task, description, source_message_url,"
    " created_at, completed_at, completed_by, recurring_id"
)


def create_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    added_by: int,
    task: str,
    *,
    description: str | None = None,
    source_message_url: str | None = None,
    recurring_id: int | None = None,
    now_ts: float | None = None,
) -> int:
    """Insert a new to do and return its ID."""
    cur = conn.execute(
        "INSERT INTO todos"
        " (guild_id, added_by, task, description, source_message_url,"
        "  recurring_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            added_by,
            task,
            description,
            source_message_url,
            recurring_id,
            time.time() if now_ts is None else now_ts,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_todos(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    status: str | None = None,
    limit: int = LIST_LIMIT,
) -> list[sqlite3.Row]:
    """Todos for ``guild_id``, newest first.

    ``status`` filters to ``pending`` or ``completed``; anything else (including
    None) returns both.
    """
    where = "guild_id = ?"
    params: list = [guild_id]
    if status == "pending":
        where += " AND completed_at IS NULL"
    elif status == "completed":
        where += " AND completed_at IS NOT NULL"
    return conn.execute(
        f"SELECT {_TODO_COLS} FROM todos WHERE {where}"
        f" ORDER BY created_at DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()


#: The only columns the board and its Complete picker actually read. Selecting
#: the full row would haul up to 200 × (500-char task + 1000-char description)
#: out of SQLite every refresh to render fifteen short lines.
_BOARD_COLS = "id, task, description, created_at, recurring_id"


def pending_todos(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = LIST_LIMIT
) -> list[sqlite3.Row]:
    """Outstanding todos, **oldest first** — the board's reading order, so the
    task that has been waiting longest sits at the top."""
    return conn.execute(
        f"SELECT {_BOARD_COLS} FROM todos"
        f" WHERE guild_id = ? AND completed_at IS NULL"
        f" ORDER BY created_at ASC LIMIT ?",
        (guild_id, int(limit)),
    ).fetchall()


def pending_count(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many todos are outstanding, uncapped by any list limit."""
    return conn.execute(
        "SELECT COUNT(*) FROM todos WHERE guild_id = ? AND completed_at IS NULL",
        (guild_id,),
    ).fetchone()[0]


def complete_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    guild_id: int,
    completed_by: int,
    *,
    now_ts: float | None = None,
) -> bool:
    """Mark a todo complete. Returns False when it's missing or already done.

    The ``completed_at IS NULL`` guard makes this idempotent under a race
    between the board button and the dashboard.
    """
    cur = conn.execute(
        "UPDATE todos SET completed_at = ?, completed_by = ?"
        " WHERE id = ? AND guild_id = ? AND completed_at IS NULL",
        (time.time() if now_ts is None else now_ts, completed_by, todo_id, guild_id),
    )
    return cur.rowcount > 0


# ── Board placement ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TodoBoard:
    """Where the guild's sticky board currently lives. Zeroes mean unposted."""

    guild_id: int
    channel_id: int = 0
    message_id: int = 0
    updated_at: float = 0.0

    @property
    def posted(self) -> bool:
        return bool(self.channel_id and self.message_id)


def get_board(conn: sqlite3.Connection, guild_id: int) -> TodoBoard:
    row = conn.execute(
        "SELECT channel_id, message_id, updated_at FROM todo_board WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is None:
        return TodoBoard(guild_id=guild_id)
    return TodoBoard(
        guild_id=guild_id,
        channel_id=row["channel_id"] or 0,
        message_id=row["message_id"] or 0,
        updated_at=row["updated_at"] or 0.0,
    )


def save_board(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    *,
    now_ts: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO todo_board (guild_id, channel_id, message_id, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET"
        "   channel_id = excluded.channel_id,"
        "   message_id = excluded.message_id,"
        "   updated_at = excluded.updated_at",
        (guild_id, channel_id, message_id, time.time() if now_ts is None else now_ts),
    )


def clear_board(conn: sqlite3.Connection, guild_id: int, *, now_ts: float | None = None) -> None:
    """Forget the placement without dropping the row, so a later post reuses it."""
    save_board(conn, guild_id, 0, 0, now_ts=now_ts)


def guilds_with_board(conn: sqlite3.Connection) -> list[int]:
    """Guild ids with a live board — the refresh loop's work list."""
    rows = conn.execute(
        "SELECT guild_id FROM todo_board WHERE channel_id != 0 AND message_id != 0"
    ).fetchall()
    return [r["guild_id"] for r in rows]
