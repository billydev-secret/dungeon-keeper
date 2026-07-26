"""Recurring todo definitions — chores that materialise a todo row on a cadence.

A recurring entry is a **reminder, not automation**: when it comes due the
spawner inserts an ordinary row into ``todos`` ("Post QOTD") and a mod does the
thing in Discord and ticks it off. The bot never performs the chore itself.

Time math is borrowed wholesale from ``scheduled_games_service.compute_next_run``
— the schema columns were named to match so the two can't drift. Wall-clock
fields (``time_of_day`` minutes, ``recur_days``) are the source of truth;
``next_run_at`` is a derived UTC-epoch cache the loop polls. The guild's fixed
``tz_offset_hours`` defines local time (no DST, matching the rest of the bot).

Every function takes ``now_ts`` explicitly so behavior is deterministic in tests.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable

from bot_modules.services.scheduled_games_service import compute_next_run
from bot_modules.services.todo_service import TASK_MAX_LEN, create_todo

log = logging.getLogger(__name__)

VALID_RECURRENCE = ("daily", "weekly")
VALID_STATUS = ("active", "paused")

DESCRIPTION_MAX_LEN = 1000

#: Weekday labels for rendering, Mon=0 (matches ``datetime.weekday()``).
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_COLS = (
    "id, guild_id, task, description, recurrence, time_of_day, recur_days,"
    " status, next_run_at, last_run_at, last_status, created_by, created_at"
)


@dataclass(frozen=True)
class RecurringTask:
    id: int
    guild_id: int
    task: str
    description: str | None = None
    recurrence: str = "daily"
    time_of_day: int = 0
    recur_days: tuple[int, ...] = ()
    status: str = "active"
    next_run_at: float | None = None
    last_run_at: float | None = None
    last_status: str | None = None
    created_by: int = 0
    created_at: float = 0.0


def _parse_days(raw) -> tuple[int, ...]:
    """Weekday set from stored JSON — tolerant of nulls and junk."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    out: set[int] = set()
    for item in parsed:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            out.add(day)
    return tuple(sorted(out))


def normalize_days(days: Iterable | None) -> tuple[int, ...]:
    """Clean a caller-supplied weekday set (dedup, sort, drop out-of-range)."""
    return _parse_days(json.dumps(list(days))) if days else ()


def _row_to_task(row: sqlite3.Row) -> RecurringTask:
    return RecurringTask(
        id=row["id"],
        guild_id=row["guild_id"],
        task=row["task"],
        description=row["description"],
        recurrence=row["recurrence"],
        time_of_day=row["time_of_day"] or 0,
        recur_days=_parse_days(row["recur_days"]),
        status=row["status"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        created_by=row["created_by"] or 0,
        created_at=row["created_at"] or 0.0,
    )


class RecurringValidationError(ValueError):
    """Caller-supplied fields don't describe a schedulable cadence."""


def validate(
    *, task: str, recurrence: str, time_of_day: int, recur_days: Iterable | None
) -> tuple[str, str, int, tuple[int, ...]]:
    """Normalise + validate the schedulable fields, or raise with a human sentence."""
    task = (task or "").strip()
    if not task:
        raise RecurringValidationError("Task cannot be empty.")
    if len(task) > TASK_MAX_LEN:
        raise RecurringValidationError(
            f"Task must be {TASK_MAX_LEN} characters or fewer."
        )
    if recurrence not in VALID_RECURRENCE:
        raise RecurringValidationError("Repeat must be daily or weekly.")
    try:
        minutes = int(time_of_day)
    except (TypeError, ValueError):
        raise RecurringValidationError("Time of day must be a number of minutes.") from None
    if not 0 <= minutes <= 24 * 60 - 1:
        raise RecurringValidationError("Time of day must be between 00:00 and 23:59.")
    days = normalize_days(recur_days)
    if recurrence == "weekly" and not days:
        raise RecurringValidationError("Pick at least one day of the week.")
    if recurrence == "daily":
        days = ()  # a daily entry's weekday set is meaningless; don't store one
    return task, recurrence, minutes, days


# ── CRUD ────────────────────────────────────────────────────────────────────


def create_recurring(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    task: str,
    recurrence: str,
    time_of_day: int,
    recur_days: Iterable | None = None,
    description: str | None = None,
    created_by: int = 0,
    offset_hours: float = 0.0,
    now_ts: float,
) -> int:
    """Insert a recurring definition with its first ``next_run_at`` computed."""
    task, recurrence, minutes, days = validate(
        task=task, recurrence=recurrence, time_of_day=time_of_day, recur_days=recur_days
    )
    next_run = compute_next_run(
        now_utc=now_ts,
        offset_hours=offset_hours,
        recurrence=recurrence,
        time_of_day_min=minutes,
        recur_days=list(days),
    )
    cur = conn.execute(
        "INSERT INTO todo_recurring"
        " (guild_id, task, description, recurrence, time_of_day, recur_days,"
        "  status, next_run_at, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (
            guild_id,
            task,
            (description or "").strip()[:DESCRIPTION_MAX_LEN] or None,
            recurrence,
            minutes,
            json.dumps(list(days)) if days else None,
            next_run,
            created_by,
            now_ts,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_recurring(conn: sqlite3.Connection, guild_id: int) -> list[RecurringTask]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM todo_recurring WHERE guild_id = ?"
        f" ORDER BY status = 'paused', time_of_day, id",
        (guild_id,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_recurring(
    conn: sqlite3.Connection, recurring_id: int, guild_id: int
) -> RecurringTask | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM todo_recurring WHERE id = ? AND guild_id = ?",
        (recurring_id, guild_id),
    ).fetchone()
    return _row_to_task(row) if row else None


def update_recurring(
    conn: sqlite3.Connection,
    recurring_id: int,
    guild_id: int,
    *,
    task: str,
    recurrence: str,
    time_of_day: int,
    recur_days: Iterable | None = None,
    description: str | None = None,
    offset_hours: float = 0.0,
    now_ts: float,
) -> bool:
    """Rewrite a definition's schedulable fields and recompute ``next_run_at``."""
    task, recurrence, minutes, days = validate(
        task=task, recurrence=recurrence, time_of_day=time_of_day, recur_days=recur_days
    )
    next_run = compute_next_run(
        now_utc=now_ts,
        offset_hours=offset_hours,
        recurrence=recurrence,
        time_of_day_min=minutes,
        recur_days=list(days),
    )
    cur = conn.execute(
        "UPDATE todo_recurring SET task = ?, description = ?, recurrence = ?,"
        " time_of_day = ?, recur_days = ?, next_run_at = ?"
        " WHERE id = ? AND guild_id = ?",
        (
            task,
            (description or "").strip()[:DESCRIPTION_MAX_LEN] or None,
            recurrence,
            minutes,
            json.dumps(list(days)) if days else None,
            next_run,
            recurring_id,
            guild_id,
        ),
    )
    return cur.rowcount > 0


def delete_recurring(conn: sqlite3.Connection, recurring_id: int, guild_id: int) -> bool:
    """Drop a definition. Rows it already spawned stay on the list — they're
    real outstanding work, and orphaning them beats silently deleting a task a
    mod may be part-way through."""
    cur = conn.execute(
        "DELETE FROM todo_recurring WHERE id = ? AND guild_id = ?",
        (recurring_id, guild_id),
    )
    return cur.rowcount > 0


def set_status(
    conn: sqlite3.Connection,
    recurring_id: int,
    guild_id: int,
    status: str,
    *,
    offset_hours: float = 0.0,
    now_ts: float,
) -> bool:
    """Pause or resume. Resuming recomputes ``next_run_at`` from now, so a long
    pause doesn't come back and immediately fire a stale slot."""
    if status not in VALID_STATUS:
        raise RecurringValidationError("Status must be active or paused.")
    task = get_recurring(conn, recurring_id, guild_id)
    if task is None:
        return False
    next_run = task.next_run_at
    if status == "active":
        next_run = compute_next_run(
            now_utc=now_ts,
            offset_hours=offset_hours,
            recurrence=task.recurrence,
            time_of_day_min=task.time_of_day,
            recur_days=list(task.recur_days),
        )
    cur = conn.execute(
        "UPDATE todo_recurring SET status = ?, next_run_at = ? WHERE id = ? AND guild_id = ?",
        (status, next_run, recurring_id, guild_id),
    )
    return cur.rowcount > 0


# ── The tick ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpawnResult:
    recurring_id: int
    guild_id: int
    status: str  # 'spawned' | 'skipped_pending'
    todo_id: int | None = None


def has_open_instance(conn: sqlite3.Connection, recurring_id: int) -> bool:
    """Whether this definition's last spawned row is still outstanding."""
    row = conn.execute(
        "SELECT 1 FROM todos WHERE recurring_id = ? AND completed_at IS NULL LIMIT 1",
        (recurring_id,),
    ).fetchone()
    return row is not None


def due_recurring(conn: sqlite3.Connection, now_ts: float) -> list[RecurringTask]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM todo_recurring"
        f" WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?"
        f" ORDER BY next_run_at",
        (now_ts,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def spawn_due(
    conn: sqlite3.Connection,
    *,
    now_ts: float,
    offset_hours_for: Callable[[int], float],
) -> list[SpawnResult]:
    """Materialise every due recurring definition into a todo row.

    **Skip-if-pending:** when the previous instance is still outstanding no
    second row is created — the definition just advances. Otherwise a chore
    nobody did all week stacks five identical rows on the board; one
    increasingly-old row is the signal you actually want.

    ``next_run_at`` advances via ``compute_next_run(after=...)``, which jumps
    past *all* missed occurrences to the next future slot. So a bot that was
    down for three days spawns one row on boot, not three.
    """
    results: list[SpawnResult] = []
    for task in due_recurring(conn, now_ts):
        offset = offset_hours_for(task.guild_id)
        if has_open_instance(conn, task.id):
            status = "skipped_pending"
            todo_id = None
        else:
            todo_id = create_todo(
                conn,
                task.guild_id,
                task.created_by,
                task.task,
                description=task.description,
                recurring_id=task.id,
                now_ts=now_ts,
            )
            status = "spawned"

        next_run = compute_next_run(
            now_utc=now_ts,
            offset_hours=offset,
            recurrence=task.recurrence,
            time_of_day_min=task.time_of_day,
            recur_days=list(task.recur_days),
            after=task.next_run_at,
        )
        conn.execute(
            "UPDATE todo_recurring SET next_run_at = ?, last_run_at = ?, last_status = ?"
            " WHERE id = ?",
            (next_run, now_ts, status, task.id),
        )
        results.append(
            SpawnResult(
                recurring_id=task.id,
                guild_id=task.guild_id,
                status=status,
                todo_id=todo_id,
            )
        )
    return results


def run_now(
    conn: sqlite3.Connection,
    recurring_id: int,
    guild_id: int,
    *,
    now_ts: float,
    offset_hours: float = 0.0,
) -> SpawnResult | None:
    """Force a definition due immediately (dashboard "Run now").

    Deliberately routed through the same due-window the loop uses rather than
    inserting directly, so skip-if-pending and the ``next_run_at`` advance
    behave identically to a natural fire.
    """
    task = get_recurring(conn, recurring_id, guild_id)
    if task is None:
        return None
    conn.execute(
        "UPDATE todo_recurring SET next_run_at = ?, status = 'active' WHERE id = ?",
        (now_ts, recurring_id),
    )
    spawned = spawn_due(conn, now_ts=now_ts, offset_hours_for=lambda _gid: offset_hours)
    for result in spawned:
        if result.recurring_id == recurring_id:
            return result
    return None


def describe_cadence(task: RecurringTask) -> str:
    """Human summary like ``Daily at 09:00`` or ``Weekly on Mon, Thu at 18:30``."""
    hh, mm = divmod(int(task.time_of_day), 60)
    when = f"{hh:02d}:{mm:02d}"
    if task.recurrence == "weekly":
        days = ", ".join(WEEKDAY_NAMES[d] for d in task.recur_days) or "no days"
        return f"Weekly on {days} at {when}"
    return f"Daily at {when}"
