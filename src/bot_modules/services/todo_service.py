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
    " created_at, completed_at, completed_by, recurring_id, missed_at,"
    " purchase_id"
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
    purchase_id: int | None = None,
    now_ts: float | None = None,
) -> int:
    """Insert a new to do and return its ID.

    ``purchase_id`` marks a row a custom-shop-item order spawned, the way
    ``recurring_id`` marks one a recurring definition spawned. The task text of
    such a row names the item and never the buyer — see
    ``economy/shop_items.todo_task_text``.
    """
    cur = conn.execute(
        "INSERT INTO todos"
        " (guild_id, added_by, task, description, source_message_url,"
        "  recurring_id, purchase_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            added_by,
            task,
            description,
            source_message_url,
            recurring_id,
            purchase_id,
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
_BOARD_COLS = (
    "t.id, t.task, t.description, t.created_at, t.recurring_id, t.purchase_id"
)

#: A purchase-spawned row names the item, never the buyer (see
#: ``economy/shop_items.todo_task_text`` for why). The buyer is joined in here
#: instead, so the board can say who an order is for while the stored text
#: stays free of them — and so an erased buyer's row simply renders as unknown,
#: because the purchase row is purged and the join finds nothing.
_BUYER_JOIN = (
    " LEFT JOIN econ_shop_purchases p ON p.id = t.purchase_id"
)

#: A row is outstanding only while it is neither ticked nor written off. The
#: ``missed_at`` half is what stops a chore the daily reset closed yesterday
#: from lingering on the all-todos board forever.
_OPEN = "completed_at IS NULL AND missed_at IS NULL"
#: The same predicate, qualified for the buyer-join query above.
_OPEN_T = "t.completed_at IS NULL AND t.missed_at IS NULL"


#: Chore-spawned rows are excluded from the board's Tasks section because the
#: Chores section above it already shows them, with richer state (done / missed
#: / streak) than a plain task line can carry. Listing them twice on one board
#: was the first thing that looked wrong when the two boards merged.
_NOT_A_CHORE = "recurring_id IS NULL"


def pending_todos(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    limit: int = LIST_LIMIT,
    exclude_chores: bool = False,
) -> list[sqlite3.Row]:
    """Outstanding todos, **oldest first** — the board's reading order, so the
    task that has been waiting longest sits at the top.

    ``exclude_chores`` drops rows a recurring definition spawned; the board
    passes it, the dashboard does not (its one list is the whole list).
    """
    where = _OPEN_T + (f" AND t.{_NOT_A_CHORE}" if exclude_chores else "")
    return conn.execute(
        f"SELECT {_BOARD_COLS}, p.user_id AS buyer_id FROM todos t"
        f"{_BUYER_JOIN}"
        f" WHERE t.guild_id = ? AND {where}"
        f" ORDER BY t.created_at ASC LIMIT ?",
        (guild_id, int(limit)),
    ).fetchall()


def pending_count(
    conn: sqlite3.Connection, guild_id: int, *, exclude_chores: bool = False
) -> int:
    """How many todos are outstanding, uncapped by any list limit."""
    where = _OPEN + (f" AND {_NOT_A_CHORE}" if exclude_chores else "")
    return conn.execute(
        f"SELECT COUNT(*) FROM todos WHERE guild_id = ? AND {where}",
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

    A row a custom-shop-item order spawned also DELIVERS that order here,
    releasing the escrowed coins to the server (and opening the rental, for a
    weekly item). It hangs off the guarded UPDATE's rowcount rather than
    guarding itself, so it inherits that exactly-once property — and it lives
    in here rather than at the two call sites so neither the board button nor
    the dashboard can tick an order off without settling it.
    """
    now = time.time() if now_ts is None else now_ts
    cur = conn.execute(
        f"UPDATE todos SET completed_at = ?, completed_by = ?"
        f" WHERE id = ? AND guild_id = ? AND {_OPEN}",
        (now, completed_by, todo_id, guild_id),
    )
    if cur.rowcount == 0:
        return False
    _settle_purchase(conn, todo_id, completed_by, now)
    return True


def _settle_purchase(
    conn: sqlite3.Connection, todo_id: int, completed_by: int, now: float
) -> None:
    """Deliver the shop order behind a just-completed todo, if there is one.

    Deferred import: the economy imports the todo board, not the other way
    round, and this keeps that true for every caller who never sells anything.
    """
    row = conn.execute(
        "SELECT purchase_id FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()
    if row is None or not row["purchase_id"]:
        return
    from bot_modules.services.economy_shop_items_service import (  # noqa: PLC0415
        fulfil_for_todo,
    )

    fulfil_for_todo(conn, todo_id, completed_by, now=now)


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
    """Where the guild's sticky board lives. Zeroes mean unposted."""

    guild_id: int
    channel_id: int = 0
    message_id: int = 0
    updated_at: float = 0.0

    @property
    def posted(self) -> bool:
        return bool(self.channel_id and self.message_id)


def get_board(conn: sqlite3.Connection, guild_id: int) -> TodoBoard:
    row = conn.execute(
        "SELECT channel_id, message_id, updated_at FROM todo_board"
        " WHERE guild_id = ?",
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
        (
            guild_id,
            channel_id,
            message_id,
            time.time() if now_ts is None else now_ts,
        ),
    )


def clear_board(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now_ts: float | None = None,
) -> None:
    """Forget the placement without dropping the row, so a later post reuses it."""
    save_board(conn, guild_id, 0, 0, now_ts=now_ts)


def guilds_with_board(conn: sqlite3.Connection) -> list[int]:
    """Guild ids with a live board — the refresh loop's work list."""
    rows = conn.execute(
        "SELECT guild_id FROM todo_board"
        " WHERE channel_id != 0 AND message_id != 0"
    ).fetchall()
    return [r["guild_id"] for r in rows]


#: What is lost while the board is unposted. It is the only Discord surface
#: that can tick anything off, so with it gone a mod can still add tasks with
#: /todo and then find nowhere to complete them. This went unsaid for three
#: days in prod and was discovered by failing to tick anything off, so the
#: dashboard says it on the card and again before removing the board.
UNPOSTED_COST = (
    "nothing can be ticked off from Discord while it is unposted — the board"
    " is the only surface with a Complete button"
)
