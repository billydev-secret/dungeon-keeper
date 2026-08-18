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

#: The two sticky boards. ``all`` is the original everything-board; ``chores``
#: lists only rows a recurring definition spawned. They are separate rows in
#: ``todo_board`` keyed ``(guild_id, kind)``.
BOARD_ALL = "all"
BOARD_CHORES = "chores"
BOARD_KINDS = (BOARD_ALL, BOARD_CHORES)

#: How each board is named to a human — used by the collision error, so the mod
#: is told *which* board already sits in the channel they picked.
BOARD_NAMES = {
    BOARD_ALL: "the server todo board",
    BOARD_CHORES: "the mod chore board",
}

#: Ceiling on a single list query — the dashboard paginates visually, not by
#: query, and the board renders far fewer than this.
LIST_LIMIT = 200

_TODO_COLS = (
    "id, guild_id, added_by, task, description, source_message_url,"
    " created_at, completed_at, completed_by, recurring_id, missed_at"
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
        where += f" AND {_OPEN}"
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

#: A row is outstanding only while it is neither ticked nor written off. The
#: ``missed_at`` half is what stops a chore the daily reset closed yesterday
#: from lingering on the all-todos board forever.
_OPEN = "completed_at IS NULL AND missed_at IS NULL"


def pending_todos(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = LIST_LIMIT
) -> list[sqlite3.Row]:
    """Outstanding todos, **oldest first** — the board's reading order, so the
    task that has been waiting longest sits at the top."""
    return conn.execute(
        f"SELECT {_BOARD_COLS} FROM todos"
        f" WHERE guild_id = ? AND {_OPEN}"
        f" ORDER BY created_at ASC LIMIT ?",
        (guild_id, int(limit)),
    ).fetchall()


def pending_count(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many todos are outstanding, uncapped by any list limit."""
    return conn.execute(
        f"SELECT COUNT(*) FROM todos WHERE guild_id = ? AND {_OPEN}",
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
    """Mark a todo complete. Returns False when it's missing or already closed.

    The ``completed_at IS NULL`` guard makes this idempotent under a race
    between the board button and the dashboard. The ``missed_at IS NULL`` half
    refuses a row the daily reset already wrote off: yesterday's chore is
    finished business, and letting a late tick credit it would both invent a
    completion that never happened and corrupt the streak either side of it.
    """
    cur = conn.execute(
        f"UPDATE todos SET completed_at = ?, completed_by = ?"
        f" WHERE id = ? AND guild_id = ? AND {_OPEN}",
        (time.time() if now_ts is None else now_ts, completed_by, todo_id, guild_id),
    )
    return cur.rowcount > 0


def mark_missed(
    conn: sqlite3.Connection, todo_id: int, *, now_ts: float
) -> bool:
    """Close an outstanding row *without* crediting it. Returns False if already closed.

    The daily reset's other half: the next occurrence of a recurring chore can
    only spawn once the previous instance stops being outstanding, and this is
    how it stops without pretending anyone did it.
    """
    cur = conn.execute(
        f"UPDATE todos SET missed_at = ? WHERE id = ? AND {_OPEN}",
        (now_ts, todo_id),
    )
    return cur.rowcount > 0


# ── Board placement ─────────────────────────────────────────────


@dataclass(frozen=True)
class TodoBoard:
    """Where one of the guild's sticky boards lives. Zeroes mean unposted."""

    guild_id: int
    kind: str = BOARD_ALL
    channel_id: int = 0
    message_id: int = 0
    updated_at: float = 0.0

    @property
    def posted(self) -> bool:
        return bool(self.channel_id and self.message_id)


def get_board(
    conn: sqlite3.Connection, guild_id: int, kind: str = BOARD_ALL
) -> TodoBoard:
    row = conn.execute(
        "SELECT channel_id, message_id, updated_at FROM todo_board"
        " WHERE guild_id = ? AND kind = ?",
        (guild_id, kind),
    ).fetchone()
    if row is None:
        return TodoBoard(guild_id=guild_id, kind=kind)
    return TodoBoard(
        guild_id=guild_id,
        kind=kind,
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
    kind: str = BOARD_ALL,
    now_ts: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO todo_board (guild_id, kind, channel_id, message_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(guild_id, kind) DO UPDATE SET"
        "   channel_id = excluded.channel_id,"
        "   message_id = excluded.message_id,"
        "   updated_at = excluded.updated_at",
        (
            guild_id,
            kind,
            channel_id,
            message_id,
            time.time() if now_ts is None else now_ts,
        ),
    )


def clear_board(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    kind: str = BOARD_ALL,
    now_ts: float | None = None,
) -> None:
    """Forget the placement without dropping the row, so a later post reuses it."""
    save_board(conn, guild_id, 0, 0, kind=kind, now_ts=now_ts)


def guilds_with_board(
    conn: sqlite3.Connection, kind: str = BOARD_ALL
) -> list[int]:
    """Guild ids with a live board of this kind — the refresh loop's work list."""
    rows = conn.execute(
        "SELECT guild_id FROM todo_board"
        " WHERE kind = ? AND channel_id != 0 AND message_id != 0",
        (kind,),
    ).fetchall()
    return [r["guild_id"] for r in rows]


#: What is lost while the all-todos board is unposted. The chore board's Mark
#: Done only ever offers *recurring* instances, so with the all-todos board
#: gone there is no Discord surface that can complete an ordinary todo at all —
#: a mod can still add one with /todo and then find nowhere to tick it off.
#: This went unsaid for three days in prod, and was discovered by failing to
#: tick anything off rather than by anything the dashboard reported.
UNPOSTED_ALL_COST = (
    "no one can complete an ordinary todo from Discord while it is unposted —"
    " the chore board's Mark Done only offers recurring chores"
)


def board_conflict_detail(resident_kind: str) -> str:
    """The 409 a mod sees when two sticky boards would share a channel.

    Names the resident and, when it is the all-todos board, what clearing it to
    make room would cost. Removing it *is* the way through this refusal, so the
    price belongs in the sentence that sends them to do it.
    """
    name = BOARD_NAMES.get(resident_kind, "another todo board")
    detail = (
        f"{name.capitalize()} is already in that channel. Two sticky boards"
        " can't share one — they'd take turns being buried. Move that one"
        " first, or pick a different channel."
    )
    if resident_kind == BOARD_ALL:
        detail += f" Note: if you clear it instead of moving it, {UNPOSTED_ALL_COST}."
    return detail


def conflicting_board(
    conn: sqlite3.Connection, guild_id: int, kind: str, channel_id: int
) -> str | None:
    """The name of the *other* todo board already in ``channel_id``, if any.

    Discord gives a channel one bottom slot, and two sticky panels cannot both
    hold it. Neither todo board sets ``restick_on_bot``, so they cannot storm
    the way the two opted-in economy panels could
    (docs/reviews/2026-08-06-sticky-panel-machinery.md F1) — but the fix for
    that storm is what makes sharing a channel *quietly* broken here. Both
    boards wake on the same human message, race for the slot, and the one that
    loses hits ``core.sticky.was_placed`` and yields. Deterministically, and
    for as long as the other keeps winning: the loser is left buried with
    nothing anyone does in the channel able to bring it back.

    So the collision is refused at configuration time, which is the only place
    it is legible to a human. Returns ``None`` when the channel is free, or the
    resident's *kind* when it is not — the caller renders it through
    ``board_conflict_detail``, which needs to know which board it is and not
    only what to call it.
    """
    if not channel_id:
        return None  # unposting can never collide with anything
    row = conn.execute(
        "SELECT kind FROM todo_board"
        " WHERE guild_id = ? AND kind != ? AND channel_id = ?",
        (guild_id, kind, int(channel_id)),
    ).fetchone()
    if row is None:
        return None
    return str(row["kind"])
