"""Sticky todo board — pure rendering and change detection.

No Discord objects and no DB here: the cog hands in plain row mappings and gets
back embed-ready text plus a signature. That keeps the board's layout and its
"has anything actually changed?" rule unit-testable without Discord mocks.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: How many pending tasks the board lists before it defers to the dashboard.
#: Discord caps a description at 4096 chars; this keeps us far under it and
#: keeps the board glanceable rather than a wall.
MAX_BOARD_ROWS = 15

#: Column width for the task cell, chosen so a row still fits on mobile.
_TASK_WIDTH = 42
_ID_WIDTH = 5

RECURRING_MARKER = "🔁"

EMPTY_BODY = "Nothing pending — the list is clear. ✨"


def _rel(ts: float) -> str:
    """A Discord relative timestamp — ticks live in every client."""
    return f"<t:{int(ts)}:R>"


def _pad(text: str, width: int) -> str:
    """Clip + left-pad a table cell for a fixed-width inline-code column.

    Columns align by wrapping cells in backticks (monospace) and padding to
    width — code blocks would align too, but they swallow the `<t:…:R>`
    timestamps, which must stay live.
    """
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


def _flatten(text: str) -> str:
    """Collapse a task onto one line so a pasted multi-line task can't break the grid."""
    return " ".join(str(text or "").split())


def render_rows(rows: Sequence[Mapping[str, Any]], *, limit: int = MAX_BOARD_ROWS) -> str:
    """The board body: one padded monospace cell per task, age outside the span.

    ``rows`` are pending todos oldest-first (see ``todo_service.pending_todos``),
    so the longest-waiting task sits at the top where it nags.
    """
    if not rows:
        return EMPTY_BODY

    lines: list[str] = []
    for row in rows[:limit]:
        marker = f" {RECURRING_MARKER}" if row.get("recurring_id") else ""
        cell = f"{_pad('#' + str(row['id']), _ID_WIDTH)}{_pad(_flatten(row['task']), _TASK_WIDTH)}"
        lines.append(f"`{cell}`{marker} {_rel(row['created_at'])}")

    hidden = len(rows) - limit
    if hidden > 0:
        lines.append(f"\n…and **{hidden}** more on the dashboard.")
    return "\n".join(lines)


def render_footer(rows: Sequence[Mapping[str, Any]]) -> str:
    pending = len(rows)
    noun = "task" if pending == 1 else "tasks"
    return f"{pending} pending {noun} · updates automatically"


def board_signature(rows: Iterable[Mapping[str, Any]]) -> tuple:
    """A hashable fingerprint of what the board *shows*.

    The refresh loop compares this against the last edit and skips the API call
    when nothing changed. Deliberately excludes ``created_at`` age text —
    ``<t:…:R>`` ticks client-side, so a board whose only change is "2h" becoming
    "3h" needs no edit at all.
    """
    return tuple(
        (row["id"], _flatten(row["task"]), bool(row.get("recurring_id")))
        for row in rows
    )


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
