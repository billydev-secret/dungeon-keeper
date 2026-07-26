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
