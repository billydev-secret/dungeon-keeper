"""Tests for bot_modules/todo/board_logic.py — the sticky board's rendering."""

from __future__ import annotations

import pytest

from bot_modules.todo.board_logic import (
    EMPTY_BODY,
    MAX_BOARD_ROWS,
    RECURRING_MARKER,
    board_signature,
    complete_option_label,
    render_footer,
    render_rows,
)

NOW = 1_800_000_000.0


def _row(todo_id: int, task: str, *, recurring_id=None, description=None, created_at=NOW):
    return {
        "id": todo_id,
        "task": task,
        "recurring_id": recurring_id,
        "description": description,
        "created_at": created_at,
    }


# ── render_rows ───────────────────────────────────────────────────────


def test_empty_list_reads_as_clear():
    assert render_rows([]) == EMPTY_BODY


def test_row_carries_id_task_and_live_timestamp():
    body = render_rows([_row(12, "Post QOTD")])
    assert "#12" in body
    assert "Post QOTD" in body
    # Ages must be live client-side timestamps, not baked-in text.
    assert f"<t:{int(NOW)}:R>" in body


def test_timestamp_stays_outside_the_code_span():
    """A code span freezes `<t:…:R>`, so the cell must close before it."""
    line = render_rows([_row(12, "Post QOTD")])
    assert line.count("`") == 2
    assert line.index("`", line.index("`") + 1) < line.index("<t:")


def test_recurring_rows_are_marked():
    body = render_rows([_row(12, "Post QOTD", recurring_id=4)])
    assert RECURRING_MARKER in body


def test_one_off_rows_are_not_marked():
    body = render_rows([_row(12, "Fix the welcome embed")])
    assert RECURRING_MARKER not in body


def test_rows_keep_their_given_order():
    body = render_rows([_row(1, "Oldest"), _row(2, "Newer")])
    assert body.index("Oldest") < body.index("Newer")


def test_long_task_is_clipped_not_wrapped():
    body = render_rows([_row(1, "x" * 200)])
    assert "…" in body
    assert len(body.splitlines()) == 1


def test_multiline_task_is_flattened_onto_one_row():
    """A pasted multi-line task must not break the monospace grid."""
    body = render_rows([_row(1, "line one\nline two\n\nline three")])
    assert len(body.splitlines()) == 1
    assert "line one line two line three" in body


def test_overflow_defers_to_the_dashboard():
    rows = [_row(i, f"Task {i}") for i in range(MAX_BOARD_ROWS + 3)]
    body = render_rows(rows)
    assert "and **3** more" in body
    assert "Task 0" in body
    assert f"Task {MAX_BOARD_ROWS + 2}" not in body


def test_exactly_at_the_cap_has_no_overflow_note():
    rows = [_row(i, f"Task {i}") for i in range(MAX_BOARD_ROWS)]
    assert "more on the dashboard" not in render_rows(rows)


def test_columns_align_across_varied_id_widths():
    body = render_rows([_row(1, "Short"), _row(12345, "Another")])
    cells = [line.split("`")[1] for line in body.splitlines()]
    assert len({len(c) for c in cells}) == 1


def test_wide_ids_are_never_truncated():
    """Regression: the id is the handle a mod reads off the board to talk about
    a task. A fixed 5-wide column rendered #10042 as "#100…", which could
    collide with a different real id — the column grows instead."""
    body = render_rows([_row(10042, "Post QOTD"), _row(7, "Short")])
    assert "#10042" in body
    assert "#100…" not in body
    cells = [line.split("`")[1] for line in body.splitlines()]
    assert len({len(c) for c in cells}) == 1


# ── render_footer ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0 pending tasks"), (1, "1 pending task"), (5, "5 pending tasks")],
)
def test_footer_counts_and_pluralizes(count, expected):
    footer = render_footer([_row(i, "x") for i in range(count)])
    assert footer.startswith(expected)


# ── board_signature ───────────────────────────────────────────────────


def test_signature_stable_across_age_only_changes():
    """`<t:…:R>` ticks client-side, so ageing alone must not force an edit."""
    a = board_signature([_row(1, "Post QOTD", created_at=NOW)])
    b = board_signature([_row(1, "Post QOTD", created_at=NOW - 86400)])
    assert a == b


@pytest.mark.parametrize(
    "changed",
    [
        [_row(1, "Post QOTD"), _row(2, "New task")],   # task added
        [],                                             # task completed
        [_row(1, "Post QOTD renamed")],                 # text changed
        [_row(2, "Post QOTD")],                         # different row, same text
        [_row(1, "Post QOTD", recurring_id=7)],         # marker appeared
    ],
)
def test_signature_changes_when_the_board_would_look_different(changed):
    base = board_signature([_row(1, "Post QOTD")])
    assert board_signature(changed) != base


def test_signature_is_hashable():
    assert isinstance(hash(board_signature([_row(1, "x")])), int)


# ── complete_option_label ─────────────────────────────────────────────


def test_option_label_carries_id_and_task():
    label, desc = complete_option_label(_row(12, "Post QOTD"))
    assert label == "#12 Post QOTD"
    assert desc == ""


def test_option_label_and_description_respect_discord_caps():
    label, desc = complete_option_label(
        _row(12, "x" * 300, description="y" * 300)
    )
    assert len(label) <= 100
    assert len(desc) <= 100


def test_option_description_is_flattened():
    _, desc = complete_option_label(_row(1, "t", description="a\nb"))
    assert desc == "a b"
