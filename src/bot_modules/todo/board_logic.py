"""Sticky todo board — pure rendering and change detection.

No Discord objects and no DB here: the cog hands in plain row mappings and gets
back embed-ready text plus a signature. That keeps the board's layout and its
"has anything actually changed?" rule unit-testable without Discord mocks.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from bot_modules.services.embeds import pad_cell, rel_ts

#: How many pending tasks the board lists before it defers to the dashboard.
#: Discord caps a description at 4096 chars; this keeps us far under it and
#: keeps the board glanceable rather than a wall.
MAX_BOARD_ROWS = 15

#: Column width for the task cell, chosen so a row still fits on mobile.
_TASK_WIDTH = 42

#: Floor for the id column. It grows to fit the widest id on the board — the id
#: is the handle a mod reads off the board to talk about a task, so it is never
#: clipped (a truncated "#100…" could collide with a different real id).
_MIN_ID_WIDTH = 5

RECURRING_MARKER = "🔁"

EMPTY_BODY = "Nothing pending — the list is clear. ✨"


def _flatten(text: str) -> str:
    """Collapse a task onto one line so a pasted multi-line task can't break the grid."""
    return " ".join(str(text or "").split())


def render_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    total: int | None = None,
    limit: int = MAX_BOARD_ROWS,
) -> str:
    """The board body: one padded monospace cell per task, age outside the span.

    ``rows`` are pending todos oldest-first (see ``todo_service.pending_todos``),
    so the longest-waiting task sits at the top where it nags. ``total`` is how
    many are pending overall — the caller fetches only a screenful, so it can't
    be inferred from ``len(rows)``.
    """
    if not rows:
        return EMPTY_BODY

    total = len(rows) if total is None else total
    shown = rows[:limit]
    if not shown:  # limit <= 0; nothing to render, so nothing to measure
        return EMPTY_BODY
    # Size the id column to the widest id actually on the board, so ids are
    # padded for alignment but never truncated.
    id_width = max(
        _MIN_ID_WIDTH, max(len(f"#{row['id']}") for row in shown) + 1
    )

    lines: list[str] = []
    for row in shown:
        marker = f" {RECURRING_MARKER}" if row.get("recurring_id") else ""
        ident = f"#{row['id']}".ljust(id_width)
        cell = ident + pad_cell(_flatten(row["task"]), _TASK_WIDTH)
        lines.append(f"`{cell}`{marker} {rel_ts(row['created_at'])}")

    hidden = total - len(shown)
    if hidden > 0:
        lines.append(f"\n…and **{hidden}** more on the dashboard.")
    return "\n".join(lines)


def render_footer(total: int) -> str:
    noun = "task" if total == 1 else "tasks"
    return f"{total} pending {noun} · updates automatically"


def board_signature(
    rows: Iterable[Mapping[str, Any]],
    total: int | None = None,
    *,
    limit: int = MAX_BOARD_ROWS,
) -> tuple:
    """A hashable fingerprint of what the board *shows*.

    The refresh loop compares this against the last edit and skips the API call
    when nothing changed. Deliberately excludes ``created_at`` age text —
    ``<t:…:R>`` ticks client-side, so a board whose only change is "2h" becoming
    "3h" needs no edit at all. ``total`` is part of the fingerprint because
    completing a task *below* the visible window still changes the footer.

    Only the first ``limit`` rows count: callers fetch a sentinel row past the
    visible window to detect overflow, and that row is never rendered.
    """
    shown = tuple(
        (row["id"], _flatten(row["task"]), bool(row.get("recurring_id")))
        for row in list(rows)[:limit]
    )
    return (shown, len(shown) if total is None else total)


def complete_option_label(row: Mapping[str, Any]) -> tuple[str, str]:
    """``(label, description)`` for one entry of the Complete select menu.

    Discord caps a select option label at 100 chars and a description at 100.
    """
    label = f"#{row['id']} {_flatten(row['task'])}"
    if len(label) > 100:
        label = label[:99] + "…"
    desc = _flatten(row.get("description") or "")
    if len(desc) > 100:
        desc = desc[:99] + "…"
    return label, desc


# ── The chore board ─────────────────────────────────────────────────────────
#
# A second board, scoped to rows a recurring definition spawned. It is a
# *scoreboard*, not a pending list: a ticked chore stays visible until the next
# reset replaces it, because "did we do the QOTD today?" cannot be answered by
# a board that deletes the answer the moment it is yes.

CHORE_DONE = "✅"
CHORE_OPEN = "⬜"
CHORE_MISSED = "❌"

#: Shown only from two consecutive days up — a "🔥 1" on every chore that
#: happened once is noise, and makes a real run harder to spot.
STREAK_MARKER = "🔥"
STREAK_MIN = 2

#: Narrower than the all-todos board's cell: these rows carry a state box in
#: front and a name plus a timestamp behind, and the line still has to survive
#: a phone.
_CHORE_WIDTH = 34

EMPTY_CHORES = "No recurring chores set up yet — add them on the dashboard. ✨"


def chore_state(row: Mapping[str, Any]) -> str:
    """``'done' | 'missed' | 'open'`` for one chore's **latest** instance.

    A definition with no instance yet reads as ``open``: it has simply not come
    round, which is indistinguishable to a mod from due-and-not-done-yet, and
    inventing a fourth state for it would earn nothing.

    ``'missed'`` is defensive rather than routine. The daily reset closes the
    old instance and opens its replacement in a single call, so the latest
    instance is normally open or done and never missed — a *previous* miss
    reaches the board through ``missed_previous`` instead (see
    ``render_chore_rows``). This branch covers a row written off by
    ``mark_missed`` with nothing spawned behind it, which the service permits.
    """
    if row.get("completed_at"):
        return "done"
    if row.get("missed_at"):
        return "missed"
    return "open"


_CHORE_BOXES = {"done": CHORE_DONE, "missed": CHORE_MISSED, "open": CHORE_OPEN}


def render_chore_rows(
    rows: Sequence[Mapping[str, Any]], *, limit: int = MAX_BOARD_ROWS
) -> str:
    """The chore board body: a state box, the chore, then who and when.

    ``rows`` come from ``todo_recurring_service.chore_board_rows`` in
    time-of-day order, each already carrying its ``streak`` and (resolved by
    the cog, which is the only layer that may touch Discord) a
    ``completed_by_name``.
    """
    if not rows:
        return EMPTY_CHORES

    shown = rows[:limit]
    if not shown:  # limit <= 0
        return EMPTY_CHORES

    lines: list[str] = []
    for row in shown:
        state = chore_state(row)
        cell = _CHORE_BOXES[state] + " " + pad_cell(_flatten(row["task"]), _CHORE_WIDTH)

        trailing: list[str] = []
        if state == "done":
            who = _flatten(row.get("completed_by_name") or "")
            when = rel_ts(row["completed_at"])
            trailing.append(f"{who} · {when}" if who else when)
        elif state == "missed":
            # Say it in words as well as in the box: a lone ❌ in a channel
            # reads as an error, not as a day that was skipped.
            trailing.append("missed")
        elif row.get("missed_previous"):
            # The reachable miss. The reset spawns today's row the moment it
            # writes yesterday's off, so the board's row is always the fresh
            # one — without this the three days nobody did the chore would show
            # as a plain ⬜ and the record the reset exists to keep would be
            # invisible on the surface built to display it.
            trailing.append(f"{CHORE_MISSED} missed last run")

        streak = int(row.get("streak") or 0)
        if streak >= STREAK_MIN:
            trailing.append(f"{STREAK_MARKER} {streak}")

        suffix = ("  " + "  ".join(trailing)) if trailing else ""
        lines.append(f"`{cell}`{suffix}")

    hidden = len(rows) - len(shown)
    if hidden > 0:
        lines.append(f"\n…and **{hidden}** more on the dashboard.")
    return "\n".join(lines)


def render_chore_footer(rows: Sequence[Mapping[str, Any]]) -> str:
    """``2 of 3 done · updates automatically`` — the scoreboard's score.

    Counts every active chore, including any past the visible limit, so the
    footer never disagrees with the dashboard about how the day went.
    """
    total = len(rows)
    if not total:
        return "no chores yet · configure on the dashboard"
    done = sum(1 for row in rows if chore_state(row) == "done")
    # Counts the same thing the rows show: a chore still open whose previous
    # run was written off. Counting ``chore_state == "missed"`` instead would
    # be a number that is always zero, because the reset never leaves the
    # latest instance in that state.
    missed = sum(
        1
        for row in rows
        if chore_state(row) == "missed"
        or (chore_state(row) == "open" and row.get("missed_previous"))
    )
    text = f"{done} of {total} done"
    if missed:
        text += f" · {missed} missed last run"
    return f"{text} · updates automatically"


def chore_signature(
    rows: Iterable[Mapping[str, Any]], *, limit: int = MAX_BOARD_ROWS
) -> tuple:
    """A fingerprint of what the chore board *shows*.

    Same rule as ``board_signature``: excludes the completion timestamp itself,
    because ``<t:…:R>`` re-renders client-side and an age ticking over is not a
    reason to spend an API call. The *state* is in, so a tick repaints; so is
    ``completed_by_name``, since a row can change hands without changing state
    if a completion is undone and redone by someone else.
    """
    rows = list(rows)
    shown = tuple(
        (
            row["recurring_id"],
            _flatten(row["task"]),
            chore_state(row),
            bool(row.get("missed_previous")),
            _flatten(row.get("completed_by_name") or ""),
            int(row.get("streak") or 0),
        )
        for row in rows[:limit]
    )
    return (shown, len(rows))
