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
from bot_modules.services.todo_service import TASK_MAX_LEN, create_todo, mark_missed

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


def normalize_days(days: Iterable | None) -> tuple[int, ...]:
    """Clean a weekday set: dedup, sort, drop anything outside Mon=0..Sun=6."""
    out: set[int] = set()
    for item in days or ():
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            out.add(day)
    return tuple(sorted(out))


def _parse_days(raw) -> tuple[int, ...]:
    """Weekday set from stored JSON — tolerant of nulls and junk."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    return normalize_days(parsed) if isinstance(parsed, list) else ()


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
    #: Set when a scheduled fire wrote off the previous instance to make room
    #: for this one — the daily reset's audit trail.
    missed_todo_id: int | None = None


def open_instance_id(conn: sqlite3.Connection, recurring_id: int) -> int | None:
    """This definition's outstanding instance, if it has one.

    Outstanding means neither ticked nor written off: a row the reset already
    marked missed is closed, and must not keep the next occurrence from
    spawning — that would reinstate skip-if-pending through the back door.
    """
    row = conn.execute(
        "SELECT id FROM todos"
        " WHERE recurring_id = ? AND completed_at IS NULL AND missed_at IS NULL"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (recurring_id,),
    ).fetchone()
    return row["id"] if row is not None else None


def has_open_instance(conn: sqlite3.Connection, recurring_id: int) -> bool:
    """Whether this definition's last spawned row is still outstanding."""
    return open_instance_id(conn, recurring_id) is not None


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

    **Reset, not skip-if-pending.** When the occurrence comes round and the
    previous instance is still outstanding, that instance is written off
    (``missed_at``) and a fresh row spawns in its place. Exactly one row per
    definition is ever outstanding, so nothing stacks — but unlike the
    skip-if-pending rule this replaced, the day that did not happen leaves a
    durable record instead of Monday's untouched row quietly masquerading as
    Tuesday's, where one tick credited both and no streak was computable.

    ``next_run_at`` advances via ``compute_next_run(after=...)``, which jumps
    past *all* missed occurrences to the next future slot. So a bot that was
    down for three days spawns one row on boot, not three — and writes off one
    instance, not three. Downtime is not evidence that a chore was skipped, so
    the register is deliberately conservative here: it records the days the bot
    was watching.
    """
    return [
        _spawn_one(
            conn,
            task,
            now_ts=now_ts,
            offset_hours=offset_hours_for(task.guild_id),
            reset_open=True,
        )
        for task in due_recurring(conn, now_ts)
    ]


def _spawn_one(
    conn: sqlite3.Connection,
    task: RecurringTask,
    *,
    now_ts: float,
    offset_hours: float,
    advance: bool = True,
    reset_open: bool = False,
) -> SpawnResult:
    """Materialise one definition.

    ``advance`` rewrites ``next_run_at`` past this occurrence — true for a
    natural fire, false for a manual "Run now", which must not disturb the
    schedule the mod configured.

    ``reset_open`` decides what an outstanding previous instance means, and the
    two callers genuinely want opposite things:

    * A **scheduled fire** (``True``) is a day boundary. Yesterday's untouched
      row is written off and today gets its own, which is what makes the chore
      board a "did we do it today?" scoreboard.
    * **"Run now"** (``False``) is a mod adding one more instance by hand. It
      keeps skip-if-pending, because pressing the button twice must not mark
      the first press missed — that would fabricate a failure out of a double
      click, and the streak would wear it.
    """
    open_id = open_instance_id(conn, task.id)
    missed_id: int | None = None
    if open_id is not None and reset_open:
        if mark_missed(conn, open_id, now_ts=now_ts):
            missed_id = open_id
        open_id = None
    if open_id is not None:
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

    if advance:
        next_run = compute_next_run(
            now_utc=now_ts,
            offset_hours=offset_hours,
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
    else:
        conn.execute(
            "UPDATE todo_recurring SET last_run_at = ?, last_status = ? WHERE id = ?",
            (now_ts, status, task.id),
        )

    return SpawnResult(
        recurring_id=task.id,
        guild_id=task.guild_id,
        status=status,
        todo_id=todo_id,
        missed_todo_id=missed_id,
    )


def run_now(
    conn: sqlite3.Connection,
    recurring_id: int,
    guild_id: int,
    *,
    now_ts: float,
    offset_hours: float = 0.0,
) -> SpawnResult | None:
    """Add one instance of a definition immediately (dashboard "Run now").

    Deliberately narrow: it touches **only this definition**, and changes
    neither its ``status`` nor its ``next_run_at``. Skip-if-pending still
    applies here (``reset_open=False``), so pressing it twice can neither stack
    duplicates nor write the first press off as missed — a manual add is not a
    day boundary.

    It does not go through ``spawn_due``: that scans every guild, so driving it
    from one guild's request would spawn other guilds' due tasks *and* rewrite
    their ``next_run_at`` using the requesting guild's UTC offset — silently
    moving another server's daily chore to the wrong wall-clock time. Leaving
    ``status`` alone likewise means "add one now" can't quietly un-pause an
    entry a mod paused for the holidays.
    """
    task = get_recurring(conn, recurring_id, guild_id)
    if task is None:
        return None
    return _spawn_one(
        conn,
        task,
        now_ts=now_ts,
        offset_hours=offset_hours,
        advance=False,
        reset_open=False,
    )


def describe_cadence(task: RecurringTask) -> str:
    """Human summary like ``Daily at 09:00`` or ``Weekly on Mon, Thu at 18:30``."""
    hh, mm = divmod(int(task.time_of_day), 60)
    when = f"{hh:02d}:{mm:02d}"
    if task.recurrence == "weekly":
        days = ", ".join(WEEKDAY_NAMES[d] for d in task.recur_days) or "no days"
        return f"Weekly on {days} at {when}"
    return f"Daily at {when}"


# ── What the chore board reads ──────────────────────────────────────────────


#: How many instances back the streak walk reads per definition. A streak ends
#: at the first missed day, so in practice the walk stops within a few rows;
#: this is only a ceiling against a chore with years of unbroken history.
STREAK_LOOKBACK = 400


def _chore_history(
    conn: sqlite3.Connection, guild_id: int, *, lookback: int
) -> dict[int, dict]:
    """Per definition: ``{"streak": int, "missed_previous": bool}``.

    Both answers come off one newest-first walk of that definition's instances,
    which is the only reason a chore board refresh is one query rather than two.

    **Today does not count against you.** The newest instance is skipped while
    it is still outstanding — the day is not over, and a chore due at 09:00
    would otherwise show a zeroed streak every morning until someone ticked it.
    A missed instance ends the walk; a completed one extends it.

    ``missed_previous`` is whether the instance *before* the current one was
    written off. It exists because the board can otherwise never show a miss:
    the reset marks the old row missed and spawns its replacement in the same
    breath, so the latest instance — the only one the board renders — is always
    open or done, never missed.

    The read is bounded per definition by ``ROW_NUMBER()``, not just the walk,
    so a chore with years of history costs the same as a new one.
    """
    rows = conn.execute(
        "SELECT rid, completed_at, missed_at FROM ("
        "  SELECT t.recurring_id AS rid, t.completed_at, t.missed_at,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY t.recurring_id"
        "           ORDER BY t.created_at DESC, t.id DESC"
        "         ) AS rn"
        "  FROM todos t JOIN todo_recurring r ON r.id = t.recurring_id"
        "  WHERE r.guild_id = ? AND t.recurring_id IS NOT NULL"
        ") WHERE rn <= ? ORDER BY rid, rn",
        (guild_id, int(lookback)),
    ).fetchall()

    out: dict[int, dict] = {}
    depth: dict[int, int] = {}
    ended: set[int] = set()
    for row in rows:
        rid = row["rid"]
        entry = out.setdefault(rid, {"streak": 0, "missed_previous": False})
        seen = depth.get(rid, 0)
        depth[rid] = seen + 1
        if rid in ended:
            continue
        if seen == 1 and row["missed_at"] is not None:
            # The instance directly behind the current one was written off.
            entry["missed_previous"] = True
        if row["completed_at"] is not None:
            entry["streak"] += 1
        elif seen == 0 and row["missed_at"] is None:
            pass  # today, still undecided — neither extends nor breaks
        else:
            ended.add(rid)  # missed, or an older row left outstanding
    return out


def chore_streaks(
    conn: sqlite3.Connection, guild_id: int, *, lookback: int = STREAK_LOOKBACK
) -> dict[int, int]:
    """``recurring_id -> consecutive completed instances``, most recent first.

    A streak is only meaningful because the reset writes off the days a chore
    did not happen: under the old skip-if-pending rule an undone chore left no
    row at all for the day it was skipped, so "6 days running" was unknowable.
    """
    return {
        rid: entry["streak"]
        for rid, entry in _chore_history(conn, guild_id, lookback=lookback).items()
    }


def chore_board_rows(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 25
) -> list[dict]:
    """One row per **active** definition: its latest instance, plus its streak.

    The chore board is a scoreboard, not a pending list, so a ticked chore stays
    on it — greyed but present — until the next reset replaces it. That is the
    whole point of the surface: "did we do it today?" cannot be answered by a
    board that removes the answer the moment it is yes.

    Paused definitions are left out. A chore a mod deliberately paused for the
    holidays is not a chore the team is failing to do, and showing it with a
    dead streak reads as a reproach.
    """
    rows = conn.execute(
        "SELECT r.id AS recurring_id, r.task AS task, r.recurrence,"
        "       r.time_of_day, r.recur_days,"
        "       t.id AS todo_id, t.created_at, t.completed_at,"
        "       t.completed_by, t.missed_at"
        " FROM todo_recurring r"
        " LEFT JOIN todos t ON t.id = ("
        "     SELECT id FROM todos WHERE recurring_id = r.id"
        "     ORDER BY created_at DESC, id DESC LIMIT 1"
        " )"
        " WHERE r.guild_id = ? AND r.status = 'active'"
        # Reading order is the order of the day, so the board scans like a
        # shift checklist rather than by an id nobody thinks in.
        " ORDER BY r.time_of_day ASC, r.id ASC"
        " LIMIT ?",
        (guild_id, int(limit)),
    ).fetchall()

    history = _chore_history(conn, guild_id, lookback=STREAK_LOOKBACK)
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        entry = history.get(row["recurring_id"]) or {}
        item["recur_days"] = _parse_days(row["recur_days"])
        item["streak"] = entry.get("streak", 0)
        # Whether the instance before the current one was written off. Without
        # this the board could never show a miss at all: the reset closes the
        # old row and opens its replacement in one call, so the latest instance
        # — the only one rendered — is never the missed one.
        item["missed_previous"] = bool(entry.get("missed_previous"))
        out.append(item)
    return out
