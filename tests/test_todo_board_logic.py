"""Tests for bot_modules/todo/board_logic.py — the sticky board's rendering."""

from __future__ import annotations

import pytest

from bot_modules.todo.board_logic import (
    CHORE_DONE,
    CHORE_MISSED,
    CHORE_OPEN,
    EMPTY_BODY,
    EMPTY_CHORES,
    MAX_BOARD_ROWS,
    RECURRING_MARKER,
    STREAK_MARKER,
    board_signature,
    chore_signature,
    chore_state,
    complete_option_label,
    nothing_to_tick_message,
    render_chore_footer,
    render_chore_rows,
    render_footer,
    render_rows,
    tickable_chores,
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


def test_overflow_counts_rows_the_caller_never_fetched():
    """The board fetches one screenful plus a sentinel, so the true pending
    count arrives separately — the note must reflect it, not len(rows)."""
    rows = [_row(i, f"Task {i}") for i in range(MAX_BOARD_ROWS + 1)]
    assert "and **85** more" in render_rows(rows, total=100)


def test_footer_is_not_capped_by_the_fetched_page():
    assert render_footer(100).startswith("100 pending tasks")


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
    assert render_footer(count).startswith(expected)


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


def test_signature_tracks_the_total_below_the_visible_window():
    """Completing a task the board never showed still changes the footer."""
    rows = [_row(i, f"Task {i}") for i in range(MAX_BOARD_ROWS)]
    assert board_signature(rows, 40) != board_signature(rows, 39)


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


def test_signature_ignores_the_unrendered_sentinel_row():
    """Callers fetch one row past the visible window to detect overflow; that
    row is never drawn, so it must not force an edit on its own."""
    rows = [_row(i, f"Task {i}") for i in range(MAX_BOARD_ROWS + 1)]
    swapped = rows[:MAX_BOARD_ROWS] + [_row(999, "Different sentinel")]
    assert board_signature(rows, 40) == board_signature(swapped, 40)


def test_render_rows_survives_a_zero_limit():
    assert render_rows([_row(1, "x")], limit=0) == EMPTY_BODY


# ── the chore board ───────────────────────────────────────────────────


def _chore(
    recurring_id: int,
    task: str,
    *,
    completed_at=None,
    completed_by_name="",
    missed_at=None,
    streak=0,
    todo_id=1,
    missed_previous=False,
    next_run_at=None,
):
    return {
        "recurring_id": recurring_id,
        "todo_id": todo_id,
        "task": task,
        "completed_at": completed_at,
        "completed_by_name": completed_by_name,
        "missed_at": missed_at,
        "streak": streak,
        "missed_previous": missed_previous,
        "next_run_at": next_run_at,
    }


@pytest.mark.parametrize(
    "row,expected",
    [
        (_chore(1, "QOTD"), "open"),
        (_chore(1, "QOTD", completed_at=NOW), "done"),
        (_chore(1, "QOTD", missed_at=NOW), "missed"),
        # Never spawned yet reads as open: not-yet-due is indistinguishable to
        # a mod from due-and-not-done, and a fourth state would earn nothing.
        (_chore(1, "QOTD", todo_id=None), "open"),
    ],
)
def test_chore_state_classifies_the_latest_instance(row, expected):
    assert chore_state(row) == expected


def test_chore_state_prefers_completion_over_a_stale_missed_stamp():
    """Belt and braces: a row can only be one or the other, but if both are
    somehow set, "someone did it" is the honest reading."""
    row = _chore(1, "QOTD", completed_at=NOW, missed_at=NOW)
    assert chore_state(row) == "done"


def test_render_chore_rows_shows_who_ticked_it_and_when():
    body = render_chore_rows([_chore(1, "Post the QOTD", completed_at=NOW,
                                     completed_by_name="Billy")])
    assert CHORE_DONE in body
    assert "Post the QOTD" in body
    assert "Billy" in body
    assert f"<t:{int(NOW)}:R>" in body


def test_render_chore_rows_marks_an_open_chore_without_a_name():
    body = render_chore_rows([_chore(1, "Post the QOTD")])
    assert CHORE_OPEN in body
    assert "·" not in body  # no who/when trailer on a row nobody has done


def test_render_chore_rows_says_missed_in_words():
    """A lone ❌ in a mod channel reads as an error, not as a skipped day."""
    body = render_chore_rows([_chore(1, "Post the QOTD", missed_at=NOW)])
    assert CHORE_MISSED in body
    assert "missed" in body


def test_render_chore_rows_shows_a_streak_from_two_up():
    body = render_chore_rows([_chore(1, "QOTD", completed_at=NOW, streak=6)])
    assert f"{STREAK_MARKER} 6" in body


@pytest.mark.parametrize("streak", [0, 1])
def test_render_chore_rows_hides_a_trivial_streak(streak):
    """A 🔥 1 on everything that happened once is noise that hides real runs."""
    body = render_chore_rows([_chore(1, "QOTD", completed_at=NOW, streak=streak)])
    assert STREAK_MARKER not in body


def test_render_chore_rows_handles_an_unresolved_member():
    """The cog could not resolve the member — show the time, not an empty name."""
    body = render_chore_rows([_chore(1, "QOTD", completed_at=NOW,
                                     completed_by_name="")])
    assert f"<t:{int(NOW)}:R>" in body
    assert " · " not in body


def test_render_chore_rows_is_empty_with_no_chores():
    assert render_chore_rows([]) == EMPTY_CHORES


def test_render_chore_rows_survives_a_zero_limit():
    assert render_chore_rows([_chore(1, "QOTD")], limit=0) == EMPTY_CHORES


def test_render_chore_rows_flattens_a_multiline_chore():
    """A pasted newline must not break the monospace grid."""
    body = render_chore_rows([_chore(1, "Post\nthe\nQOTD")])
    assert "Post the QOTD" in body
    assert body.count("\n") == 0


def test_render_chore_rows_defers_overflow_to_the_dashboard():
    rows = [_chore(n, f"Chore {n}") for n in range(MAX_BOARD_ROWS + 3)]
    body = render_chore_rows(rows)
    assert "and **3** more" in body


def test_chore_footer_scores_the_day():
    rows = [
        _chore(1, "A", completed_at=NOW),
        _chore(2, "B", completed_at=NOW),
        _chore(3, "C"),
    ]
    assert render_chore_footer(rows).startswith("2 of 3 done")


def test_chore_footer_calls_out_misses():
    rows = [_chore(1, "A", completed_at=NOW), _chore(2, "B", missed_at=NOW)]
    footer = render_chore_footer(rows)
    assert "1 of 2 done" in footer
    assert "1 missed" in footer


def test_chore_footer_counts_chores_past_the_visible_window():
    """The footer must not disagree with the dashboard about how the day went."""
    rows = [_chore(n, f"Chore {n}", completed_at=NOW) for n in range(MAX_BOARD_ROWS + 5)]
    assert render_chore_footer(rows).startswith(
        f"{MAX_BOARD_ROWS + 5} of {MAX_BOARD_ROWS + 5} done"
    )


def test_chore_footer_with_no_chores():
    assert "no chores yet" in render_chore_footer([])


def test_chore_signature_is_hashable():
    hash(chore_signature([_chore(1, "QOTD")]))


@pytest.mark.parametrize(
    "changed",
    [
        _chore(1, "QOTD", completed_at=NOW),          # ticked
        _chore(1, "QOTD", missed_at=NOW),             # written off
        _chore(1, "Renamed"),                          # definition renamed
        _chore(2, "QOTD"),                             # different definition
        _chore(1, "QOTD", streak=4),                   # streak moved
    ],
)
def test_chore_signature_changes_when_the_board_would_look_different(changed):
    assert chore_signature([_chore(1, "QOTD")]) != chore_signature([changed])


def test_chore_signature_ignores_the_completion_timestamp():
    """``<t:…:R>`` re-renders client-side; an age ticking over is not an edit."""
    a = _chore(1, "QOTD", completed_at=NOW, completed_by_name="Billy")
    b = _chore(1, "QOTD", completed_at=NOW + 9999, completed_by_name="Billy")
    assert chore_signature([a]) == chore_signature([b])


def test_chore_signature_tracks_who_completed_it():
    """A row can change hands without changing state if a tick is undone and redone."""
    a = _chore(1, "QOTD", completed_at=NOW, completed_by_name="Billy")
    b = _chore(1, "QOTD", completed_at=NOW, completed_by_name="Sam")
    assert chore_signature([a]) != chore_signature([b])


def test_chore_signature_tracks_chores_past_the_visible_window():
    """A chore added below the fold still changes the footer's total."""
    rows = [_chore(n, f"Chore {n}") for n in range(MAX_BOARD_ROWS)]
    assert chore_signature(rows) != chore_signature(rows + [_chore(99, "Extra")])


def test_render_chore_rows_reports_a_previous_miss_on_an_open_row():
    """The only way a miss reaches the board.

    The reset closes yesterday's row and opens today's in one call, so the row
    being rendered is always the fresh one. Before this, three consecutive
    undone days rendered as a plain ⬜ and the footer's missed count was
    structurally always zero.
    """
    body = render_chore_rows([_chore(1, "Post the QOTD", missed_previous=True)])
    assert CHORE_OPEN in body
    assert CHORE_MISSED in body
    assert "missed last run" in body


def test_a_previous_miss_is_not_shown_once_the_chore_is_done_again():
    """Ticking it today answers the nag — leaving it up would shame a fixed row."""
    body = render_chore_rows([
        _chore(1, "Post the QOTD", completed_at=NOW,
               completed_by_name="Billy", missed_previous=True)
    ])
    assert CHORE_DONE in body
    assert "missed last run" not in body


def test_chore_footer_counts_previous_misses():
    rows = [
        _chore(1, "A", completed_at=NOW),
        _chore(2, "B", missed_previous=True),
        _chore(3, "C"),
    ]
    footer = render_chore_footer(rows)
    assert "1 of 3 done" in footer
    assert "1 missed last run" in footer


def test_chore_footer_ignores_a_previous_miss_that_was_since_done():
    rows = [_chore(1, "A", completed_at=NOW, missed_previous=True)]
    assert "missed" not in render_chore_footer(rows)


def test_chore_signature_tracks_a_previous_miss():
    """A miss appearing or clearing changes the row, so it must repaint."""
    assert chore_signature([_chore(1, "A")]) != chore_signature(
        [_chore(1, "A", missed_previous=True)]
    )


# ── what Mark Done can offer, and what it says when it can't ──────────


def test_tickable_chores_excludes_a_definition_with_no_instance():
    """The disagreement this pins: the board draws a chore with no instance ⬜
    open (it has simply not come round), and the button cannot tick it because
    there is no todo row behind it. Both readings are right — what was wrong
    was the button then claiming everything was done."""
    waiting = _chore(1, "Monday prompt", todo_id=None, next_run_at=NOW)
    assert chore_state(waiting) == "open"
    assert tickable_chores([waiting]) == []


def test_tickable_chores_offers_only_open_instances():
    rows = [
        _chore(1, "Open", todo_id=10),
        _chore(2, "Done", todo_id=11, completed_at=NOW),
        _chore(3, "Missed", todo_id=12, missed_at=NOW),
        _chore(4, "Waiting", todo_id=None, next_run_at=NOW),
    ]
    assert [r["task"] for r in tickable_chores(rows)] == ["Open"]


def test_nothing_to_tick_says_when_the_first_one_lands():
    """Not "already ticked off" — nothing has come round yet, and a mod reading
    that over a board full of ⬜ concludes the button is broken."""
    rows = [
        _chore(1, "Run any game somewhere", todo_id=None, next_run_at=NOW + 7200),
        _chore(2, "Do a QOTD", todo_id=None, next_run_at=NOW + 3600),
    ]
    message = nothing_to_tick_message(rows)
    assert "Nothing due yet" in message
    # The soonest of them, not whichever the query happened to return first.
    assert "Do a QOTD" in message
    assert f"<t:{int(NOW + 3600)}:R>" in message


def test_nothing_to_tick_still_says_done_when_everything_is_done():
    rows = [_chore(1, "Do a QOTD", todo_id=10, completed_at=NOW)]
    assert nothing_to_tick_message(rows) == "Every chore is already ticked off. ✨"


def test_nothing_to_tick_on_an_empty_board_points_at_the_dashboard():
    """No chores at all is not "all ticked off" either."""
    assert nothing_to_tick_message([]) == EMPTY_CHORES
