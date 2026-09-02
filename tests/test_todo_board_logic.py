"""Tests for bot_modules/todo/board_logic.py — the sticky board's rendering."""

from __future__ import annotations

import pytest

from bot_modules.todo.board_logic import (
    CHORE_DONE,
    CHORE_MISSED,
    CHORE_OPEN,
    EMPTY_BODY,
    EMPTY_CHORES,
    EMPTY_APPROVALS,
    EMPTY_CONFESSIONS,
    EMPTY_SIGNOFFS,
    MAX_APPROVAL_ROWS,
    MAX_BOARD_ROWS,
    MAX_CHORE_ROWS,
    MAX_CONFESSION_ROWS,
    MAX_SIGNOFF_ROWS,
    ORDER_MARKER,
    RECURRING_MARKER,
    STREAK_MARKER,
    APPROVAL_HEADING,
    CHORE_HEADING,
    CONFESSION_HEADING,
    EMPTY_BOARD,
    MIN_TASK_ROWS,
    SIGNOFF_HEADING,
    TASK_HEADING,
    approval_signature,
    board_content_signature,
    board_signature,
    chore_signature,
    completable_options,
    confession_signature,
    chore_state,
    complete_option_label,
    nothing_to_tick_message,
    render_chore_footer,
    render_chore_rows,
    render_board,
    render_board_footer,
    render_footer,
    render_rows,
    render_approval_rows,
    render_confession_rows,
    render_signoff_rows,
    signoff_signature,
    task_row_budget,
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


def test_row_carries_the_id_and_the_task():
    body = render_rows([_row(12, "Post QOTD")])
    assert "#12" in body
    assert "Post QOTD" in body


def test_only_the_id_is_monospace():
    """Everything else flows, so a short task is a short line on a phone."""
    line = render_rows([_row(12, "Post QOTD")])
    assert line.count("`") == 2
    assert line.split("`")[1] == "#12"


def test_task_rows_carry_no_age():
    """Measured on the production board, a relative age costs ~9 extra wrapped
    phone lines across 13 tasks — "2 months ago" pushes almost every short row
    over a phone's width by itself. The list is oldest-first, so position
    already says what has waited longest."""
    assert "<t:" not in render_rows([_row(12, "Post QOTD")])


def test_a_short_task_fits_a_phone_line():
    """The whole point of the reflow: 34 characters is about a phone's width."""
    assert len(render_rows([_row(12, "more qotd prompts")])) <= 34


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


def test_ids_are_never_padded_out():
    """Nothing is spent on alignment any more — a short id is a short chip, so
    a short row stays a short row."""
    body = render_rows([_row(1, "Short"), _row(12345, "Another")])
    cells = [line.split("`")[1] for line in body.splitlines()]
    assert cells == ["#1", "#12345"]


def test_wide_ids_are_never_truncated():
    """The id is the handle a mod reads off the board to talk about a task. A
    fixed 5-wide column once rendered #10042 as "#100…", which could collide
    with a different real id."""
    body = render_rows([_row(10042, "Post QOTD"), _row(7, "Short")])
    assert "#10042" in body
    assert "#100…" not in body


def test_a_long_task_is_clipped_rather_than_owning_the_screen():
    body = render_rows([_row(1, "x" * 200)])
    assert body.endswith("…")
    assert len(body) < 60


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


# ── the combined board (migration 180) ────────────────────────────────


def test_both_sections_appear_under_their_headings():
    body = render_board(
        [_chore(1, "Do a QOTD")], [_row(7, "fix the quote bot")], task_total=1
    )
    assert CHORE_HEADING in body
    assert TASK_HEADING in body
    assert "Do a QOTD" in body
    assert "fix the quote bot" in body
    # Chores first: the daily scoreboard is the glanceable half.
    assert body.index(CHORE_HEADING) < body.index(TASK_HEADING)


def test_a_guild_with_no_chores_gets_no_chore_heading():
    """A heading over an empty-state sentence reads as broken."""
    body = render_board([], [_row(7, "fix the quote bot")], task_total=1)
    assert CHORE_HEADING not in body
    assert EMPTY_CHORES not in body
    assert "fix the quote bot" in body


def test_a_guild_with_no_tasks_gets_no_task_heading():
    body = render_board([_chore(1, "Do a QOTD")], [], task_total=0)
    assert TASK_HEADING not in body
    assert EMPTY_BODY not in body
    assert "Do a QOTD" in body


def test_a_wholly_empty_board_says_so_once():
    assert render_board([], [], task_total=0) == EMPTY_BOARD


def test_chores_cannot_crowd_the_tasks_off_the_board():
    """The failure the merge was meant to end, pointing the other way."""
    chores = [_chore(n, f"Chore {n}", todo_id=n) for n in range(20)]
    tasks = [_row(n, f"Task {n}") for n in range(10)]
    body = render_board(chores, tasks, task_total=10)
    assert "Task 0" in body
    for n in range(MIN_TASK_ROWS):
        assert f"Task {n}" in body


@pytest.mark.parametrize(
    ("chores_shown", "expected"),
    [
        pytest.param(0, MAX_BOARD_ROWS, id="no-chores-full-budget"),
        pytest.param(5, MAX_BOARD_ROWS - 5, id="chores-take-their-slice"),
        pytest.param(MAX_BOARD_ROWS, MIN_TASK_ROWS, id="floor-holds"),
        pytest.param(99, MIN_TASK_ROWS, id="floor-holds-past-the-limit"),
    ],
)
def test_task_row_budget(chores_shown, expected):
    assert task_row_budget(chores_shown) == expected


# ── the combined footer ───────────────────────────────────────────────


def test_footer_reports_both_halves():
    footer = render_board_footer(
        [_chore(1, "A", completed_at=NOW), _chore(2, "B")], 25
    )
    assert "1 of 2 chores done" in footer
    assert "25 tasks" in footer


def test_footer_drops_the_chore_half_when_there_are_none():
    """'0 of 0 chores done' is noise in a guild that configured none."""
    footer = render_board_footer([], 25)
    assert "chores" not in footer
    assert "25 tasks" in footer


@pytest.mark.parametrize(
    ("total", "expected"),
    [pytest.param(1, "1 task", id="singular"), pytest.param(2, "2 tasks", id="plural")],
)
def test_footer_pluralises_tasks(total, expected):
    assert expected in render_board_footer([], total)


# ── the combined signature ────────────────────────────────────────────


def test_board_signature_is_stable_across_age_only_changes():
    """<t:…:R> ticks client-side, so an age change is not worth an API call."""
    first = board_content_signature(
        [_chore(1, "A")], [_row(7, "fix it", created_at=NOW)], 1
    )
    later = board_content_signature(
        [_chore(1, "A")], [_row(7, "fix it", created_at=NOW - 9999)], 1
    )
    assert first == later


@pytest.mark.parametrize(
    ("chores", "tasks", "total"),
    [
        pytest.param([_chore(1, "A", completed_at=NOW)], [_row(7, "fix it")], 1,
                     id="a-chore-was-ticked"),
        pytest.param([_chore(1, "A")], [_row(7, "renamed")], 1, id="a-task-changed"),
        pytest.param([_chore(1, "A")], [_row(7, "fix it")], 9, id="the-total-moved"),
    ],
)
def test_board_signature_changes_when_the_board_would_look_different(
    chores, tasks, total
):
    base = board_content_signature([_chore(1, "A")], [_row(7, "fix it")], 1)
    assert board_content_signature(chores, tasks, total) != base


def test_board_signature_is_hashable():
    assert hash(board_content_signature([_chore(1, "A")], [_row(7, "x")], 1))


# ── one Complete button over both sections ────────────────────────────


def test_completable_offers_chores_first_then_tasks():
    options = completable_options(
        [_chore(1, "Do a QOTD", todo_id=42)], [_row(7, "fix the quote bot")]
    )
    assert [o["id"] for o in options] == [42, 7]


def test_a_chore_is_offered_by_its_todo_id_not_its_definition_id():
    """The thing being completed is a todo row either way."""
    options = completable_options([_chore(99, "Do a QOTD", todo_id=42)], [])
    assert options[0]["id"] == 42


def test_a_ticked_chore_is_not_offered_again():
    options = completable_options(
        [_chore(1, "Done already", todo_id=42, completed_at=NOW)], []
    )
    assert options == []


def test_a_chore_with_no_instance_yet_is_not_offered():
    options = completable_options([_chore(1, "Not due", todo_id=None)], [])
    assert options == []


def test_chores_cannot_fill_the_whole_complete_picker():
    """Discord caps a select at 25 options. Uncapped chores would fill it in a
    guild with many, leaving no way to tick an ordinary task off in Discord —
    the capability the merge existed to restore."""
    chores = [_chore(n, f"Chore {n}", todo_id=1000 + n) for n in range(30)]
    tasks = [_row(n, f"Task {n}") for n in range(5)]
    options = completable_options(chores, tasks)
    assert len(options) - MAX_CHORE_ROWS == len(tasks)
    assert any(o["task"].startswith("Task") for o in options[:25])


# ── shop orders on the board ──────────────────────────────────────────


def _order(todo_id: int, task: str, *, buyer_name="", purchase_id=7):
    row = _row(todo_id, task)
    row["purchase_id"] = purchase_id
    row["buyer_name"] = buyer_name
    return row


def test_an_order_is_marked_and_says_who_it_is_for():
    body = render_rows([_order(1, "Deliver Custom Emoji", buyer_name="Billy")])
    assert ORDER_MARKER in body
    assert "for Billy" in body


def test_the_buyer_is_outside_the_code_span():
    """The name is live text, not part of the aligned cell."""
    body = render_rows([_order(1, "Deliver Custom Emoji", buyer_name="Billy")])
    cell = body.split("`")[1]
    assert "Billy" not in cell


def test_an_erased_buyer_leaves_the_order_readable():
    """The purchase row is purged, so the join finds nobody — the work stands."""
    body = render_rows([_order(1, "Deliver Custom Emoji", buyer_name="")])
    assert "Deliver Custom Emoji" in body
    assert " · for " not in body


def test_an_ordinary_task_carries_no_order_marker():
    assert ORDER_MARKER not in render_rows([_row(1, "fix the quote bot")])


def test_a_renamed_buyer_repaints_the_board():
    """Same row, different nickname — the board says something different."""
    before = board_signature([_order(1, "Deliver X", buyer_name="Billy")])
    after = board_signature([_order(1, "Deliver X", buyer_name="Bill")])
    assert before != after


def test_becoming_an_order_repaints_the_board():
    assert board_signature([_order(1, "Deliver X")]) != board_signature(
        [_row(1, "Deliver X")]
    )


# ── quest sign-offs on the board ──────────────────────────────────────


def _claim(claim_id: int, quest="Post a selfie", *, who="Alex", reward=500):
    return {
        "id": claim_id,
        "user_id": 500 + claim_id,
        "quest_title": quest,
        "reward": reward,
        "criteria": "Do the thing",
        "claimant_name": who,
    }


def test_a_signoff_row_names_the_member_the_quest_and_the_reward():
    body = render_signoff_rows([_claim(1)], currency_emoji="🪙")
    assert "**Alex**" in body
    assert "Post a selfie" in body
    assert "🪙 500" in body


def test_a_signoff_row_carries_no_id():
    """The ids on this board belong to todos. A claim id printed the same way
    invites the wrong `#14` being ticked off the list below it."""
    assert "#" not in render_signoff_rows([_claim(14)])


def test_a_signoff_reward_is_thousands_separated():
    assert "🪙 12,500" in render_signoff_rows([_claim(1, reward=12500)])


def test_the_guilds_own_currency_emoji_is_used():
    assert "💎 500" in render_signoff_rows([_claim(1)], currency_emoji="💎")


def test_a_claimant_who_left_is_never_a_raw_id():
    body = render_signoff_rows([_claim(1, who="")])
    assert "someone" in body
    assert "501" not in body


def test_a_long_quest_title_is_clipped():
    body = render_signoff_rows([_claim(1, "x" * 200)])
    assert "…" in body
    assert len(body) < 120


def test_signoffs_keep_their_given_order():
    body = render_signoff_rows([_claim(1, "First"), _claim(2, "Second")])
    assert body.index("First") < body.index("Second")


def test_signoff_overflow_defers_to_the_dashboard():
    rows = [_claim(n, f"Quest {n}") for n in range(MAX_SIGNOFF_ROWS + 3)]
    body = render_signoff_rows(rows, total=len(rows))
    assert body.count("\n") == MAX_SIGNOFF_ROWS + 1  # rows + the overflow note
    assert "**3** more" in body


def test_no_signoffs_reads_as_clear():
    assert render_signoff_rows([]) == EMPTY_SIGNOFFS
    assert render_signoff_rows([_claim(1)], limit=0) == EMPTY_SIGNOFFS


# ── the sign-off section on the combined board ────────────────────────


def test_signoffs_lead_the_board():
    """Somebody is waiting on the mods for these; the other two sections are
    the server's own work."""
    body = render_board(
        [_chore(1, "QOTD")], [_row(1, "fix the bot")], signoff_rows=[_claim(1)]
    )
    assert body.index(SIGNOFF_HEADING) < body.index(CHORE_HEADING)
    assert body.index(CHORE_HEADING) < body.index(TASK_HEADING)


def test_the_signoff_section_is_omitted_when_empty():
    body = render_board([], [_row(1, "fix the bot")])
    assert SIGNOFF_HEADING not in body
    assert body.startswith(TASK_HEADING)


def test_signoffs_alone_still_render_a_board():
    body = render_board([], [], signoff_rows=[_claim(1)])
    assert SIGNOFF_HEADING in body
    assert body != EMPTY_BOARD


def test_signoffs_never_starve_the_task_list():
    """A backlog of claims must not push the tasks off the board — the same
    floor the chores are held to."""
    claims = [_claim(n) for n in range(MAX_SIGNOFF_ROWS + 5)]
    chores = [_chore(n, f"Chore {n}", todo_id=n) for n in range(MAX_CHORE_ROWS)]
    tasks = [_row(n, f"Task {n}") for n in range(MAX_BOARD_ROWS)]
    body = render_board(chores, tasks, signoff_rows=claims)
    for n in range(MIN_TASK_ROWS):
        assert f"Task {n}" in body


def test_the_signoff_section_takes_a_bounded_slice():
    claims = [_claim(n, f"Quest {n}") for n in range(MAX_SIGNOFF_ROWS + 4)]
    body = render_board([], [], signoff_rows=claims, signoff_total=len(claims))
    assert f"Quest {MAX_SIGNOFF_ROWS}" not in body
    assert "**4** more" in body


def test_the_footer_counts_waiting_signoffs():
    assert "1 sign-off waiting" in render_board_footer([], 3, 1)
    assert "2 sign-offs waiting" in render_board_footer([], 3, 2)


def test_the_footer_drops_the_signoff_half_when_none_wait():
    assert "sign-off" not in render_board_footer([], 3, 0)


def test_the_footer_still_reports_the_other_sections():
    footer = render_board_footer([_chore(1, "QOTD")], 3, 1)
    assert "1 sign-off waiting" in footer
    assert "chores done" in footer
    assert "3 tasks" in footer


# ── the sign-off signature ────────────────────────────────────────────


def test_a_new_claim_repaints_the_board():
    before = board_content_signature([], [], 0, signoff_rows=[], signoff_total=0)
    after = board_content_signature(
        [], [], 0, signoff_rows=[_claim(1)], signoff_total=1
    )
    assert before != after


def test_a_resolved_claim_repaints_the_board():
    before = board_content_signature(
        [], [], 0, signoff_rows=[_claim(1), _claim(2)], signoff_total=2
    )
    after = board_content_signature(
        [], [], 0, signoff_rows=[_claim(1)], signoff_total=1
    )
    assert before != after


def test_a_renamed_claimant_repaints_the_board():
    assert signoff_signature([_claim(1, who="Alex")]) != signoff_signature(
        [_claim(1, who="Alexandra")]
    )


def test_the_signoff_signature_is_hashable():
    assert isinstance(hash(board_content_signature([], [], 0, signoff_rows=[_claim(1)])), int)


def test_the_signoff_signature_ignores_the_unrendered_sentinel_row():
    """The cog fetches one row past the window to detect overflow; a change to
    that invisible row is not a reason to spend an API call."""
    rows = [_claim(n) for n in range(MAX_SIGNOFF_ROWS + 1)]
    other = rows[:-1] + [_claim(99, "Something else entirely")]
    assert signoff_signature(rows, MAX_SIGNOFF_ROWS + 1) == signoff_signature(
        other, MAX_SIGNOFF_ROWS + 1
    )


def test_a_claim_below_the_window_still_moves_the_footer():
    rows = [_claim(n) for n in range(MAX_SIGNOFF_ROWS)]
    assert signoff_signature(rows, 6) != signoff_signature(rows, 7)


# ── paid requests ─────────────────────────────────────────────────────
#
# The three paid-submission queues (a themed day, a sponsored question, a pin)
# used to post their approval cards into the economy's bank channel, which in
# the main guild is a member-facing explainer. They are on the board now, in
# one section, for the same reason the sign-offs are.


def _approval(sub_id, summary="Cursed Cooking", *, kind="theme", price=300,
              who="Alex"):
    return {
        "kind": kind,
        "id": sub_id,
        "user_id": 600 + sub_id,
        "price": price,
        "summary": summary,
        "requester_name": who,
    }


def test_an_approval_row_names_the_member_the_request_and_the_price():
    body = render_approval_rows([_approval(1)], currency_emoji="🪙")
    assert "**Alex**" in body
    assert "Cursed Cooking" in body
    assert "🪙 300" in body


def test_an_approval_row_says_which_queue_it_came_from():
    """One section over three products, so a mod must be able to tell a pin
    from a themed day before they open it."""
    body = render_approval_rows(
        [_approval(1, kind="theme"), _approval(2, "Raid at eight", kind="pin")]
    )
    assert "Theme" in body
    assert "Pin" in body


def test_an_approval_row_carries_no_id():
    assert "#" not in render_approval_rows([_approval(14)])


def test_an_approval_price_is_thousands_separated():
    assert "🪙 12,500" in render_approval_rows([_approval(1, price=12500)])


def test_an_approval_uses_the_guilds_own_currency_emoji():
    assert "💎 300" in render_approval_rows([_approval(1)], currency_emoji="💎")


def test_a_requester_who_left_is_never_a_raw_id():
    body = render_approval_rows([_approval(1, who="")])
    assert "someone" in body
    assert "601" not in body


def test_a_long_request_is_clipped():
    body = render_approval_rows([_approval(1, "x" * 200)])
    assert "…" in body
    assert len(body) < 140


def test_approvals_keep_their_given_order():
    body = render_approval_rows([_approval(1, "First"), _approval(2, "Second")])
    assert body.index("First") < body.index("Second")


def test_approval_overflow_defers_to_the_dashboard():
    rows = [_approval(n, f"Request {n}") for n in range(MAX_APPROVAL_ROWS + 3)]
    body = render_approval_rows(rows, total=len(rows))
    assert body.count("\n") == MAX_APPROVAL_ROWS + 1  # rows + the overflow note
    assert "**3** more" in body


def test_no_approvals_reads_as_clear():
    assert render_approval_rows([]) == EMPTY_APPROVALS
    assert render_approval_rows([_approval(1)], limit=0) == EMPTY_APPROVALS


# ── the paid-requests section on the combined board ───────────────────


def test_approvals_sit_under_the_signoffs_and_above_the_chores():
    body = render_board(
        [_chore(1, "QOTD")],
        [_row(1, "fix the bot")],
        signoff_rows=[_claim(1)],
        approval_rows=[_approval(1)],
    )
    assert body.index(SIGNOFF_HEADING) < body.index(APPROVAL_HEADING)
    assert body.index(APPROVAL_HEADING) < body.index(CHORE_HEADING)


def test_the_approval_section_is_omitted_when_empty():
    body = render_board([], [_row(1, "fix the bot")])
    assert APPROVAL_HEADING not in body


def test_approvals_alone_still_render_a_board():
    body = render_board([], [], approval_rows=[_approval(1)])
    assert APPROVAL_HEADING in body
    assert body != EMPTY_BOARD


def test_approvals_never_starve_the_task_list():
    approvals = [_approval(n) for n in range(MAX_APPROVAL_ROWS + 5)]
    claims = [_claim(n) for n in range(MAX_SIGNOFF_ROWS + 5)]
    chores = [_chore(n, f"Chore {n}", todo_id=n) for n in range(MAX_CHORE_ROWS)]
    tasks = [_row(n, f"Task {n}") for n in range(MAX_BOARD_ROWS)]
    body = render_board(
        chores, tasks, signoff_rows=claims, approval_rows=approvals
    )
    for n in range(MIN_TASK_ROWS):
        assert f"Task {n}" in body


def test_the_approval_section_takes_a_bounded_slice():
    rows = [_approval(n, f"Request {n}") for n in range(MAX_APPROVAL_ROWS + 4)]
    body = render_board([], [], approval_rows=rows, approval_total=len(rows))
    assert f"Request {MAX_APPROVAL_ROWS}" not in body
    assert "**4** more" in body


def test_the_footer_counts_waiting_paid_requests():
    assert "1 paid request waiting" in render_board_footer([], 3, 0, 1)
    assert "2 paid requests waiting" in render_board_footer([], 3, 0, 2)


def test_the_footer_drops_the_approval_half_when_none_wait():
    assert "paid request" not in render_board_footer([], 3, 1, 0)


# ── the paid-requests signature ───────────────────────────────────────


def test_a_new_paid_request_repaints_the_board():
    before = board_content_signature([], [], 0, approval_rows=[], approval_total=0)
    after = board_content_signature(
        [], [], 0, approval_rows=[_approval(1)], approval_total=1
    )
    assert before != after


def test_a_resolved_paid_request_repaints_the_board():
    before = board_content_signature(
        [], [], 0, approval_rows=[_approval(1), _approval(2)], approval_total=2
    )
    after = board_content_signature(
        [], [], 0, approval_rows=[_approval(1)], approval_total=1
    )
    assert before != after


def test_a_renamed_requester_repaints_the_board():
    assert approval_signature([_approval(1, who="Alex")]) != approval_signature(
        [_approval(1, who="Alexandra")]
    )


# ── confessions awaiting approval ─────────────────────────────────────
#
# The third "somebody is waiting on the mods" section. Everything below that
# looks like a repeat of the paid-request tests is testing the one thing that
# is genuinely different: this section is handed no author id at all, because
# the board's audience is the moderator tier and naming a confession's author
# is admin-only.


def _pending(pending_id, content="I ate the last biscuit", *, created_at=NOW):
    return {"id": pending_id, "content": content, "created_at": created_at}


def test_a_confession_row_shows_the_body_and_how_long_it_waited():
    body = render_confession_rows([_pending(1)])
    assert "I ate the last biscuit" in body
    assert f"<t:{int(NOW)}:R>" in body


def test_a_confession_row_carries_no_id():
    assert "#" not in render_confession_rows([_pending(14)])


def test_a_confession_row_cannot_print_an_author():
    """The row builder is given no author id, so there is nothing to leak.

    This is the guard for the whole privacy seam: if someone ever adds
    ``author_id`` to ``pending_confessions``, the fix has to be deliberate
    rather than something a renderer quietly picks up.
    """
    row = _pending(1)
    row["author_id"] = 424242
    assert "424242" not in render_confession_rows([row])


def test_a_long_confession_is_clipped():
    body = render_confession_rows([_pending(1, "x" * 400)])
    assert "…" in body
    assert len(body) < 140


def test_a_multiline_confession_cannot_break_the_grid():
    body = render_confession_rows([_pending(1, "one\ntwo\nthree")])
    assert body.count("\n") == 0


def test_an_empty_confession_still_renders_a_row():
    assert render_confession_rows([_pending(1, "")]).strip() != ""


def test_confessions_keep_their_given_order():
    body = render_confession_rows([_pending(1, "First"), _pending(2, "Second")])
    assert body.index("First") < body.index("Second")


def test_confession_overflow_does_not_point_at_a_dashboard():
    """There is deliberately no dashboard queue for these, so the overflow line
    must not send a mod looking for one."""
    rows = [_pending(n, f"Confession {n}") for n in range(MAX_CONFESSION_ROWS + 3)]
    body = render_confession_rows(rows, total=len(rows))
    assert "**3** more" in body
    assert "dashboard" not in body


def test_no_confessions_reads_as_clear():
    assert render_confession_rows([]) == EMPTY_CONFESSIONS
    assert render_confession_rows([_pending(1)], limit=0) == EMPTY_CONFESSIONS


# ── the confessions section on the combined board ─────────────────────


def test_confessions_sit_under_the_paid_requests_and_above_the_chores():
    body = render_board(
        [_chore(1, "QOTD")],
        [_row(1, "fix the bot")],
        signoff_rows=[_claim(1)],
        approval_rows=[_approval(1)],
        confession_rows=[_pending(1)],
    )
    assert body.index(APPROVAL_HEADING) < body.index(CONFESSION_HEADING)
    assert body.index(CONFESSION_HEADING) < body.index(CHORE_HEADING)


def test_the_confession_section_is_omitted_when_empty():
    """A guild with approval off never has a row, and must never see a heading."""
    body = render_board([], [_row(1, "fix the bot")])
    assert CONFESSION_HEADING not in body


def test_confessions_alone_still_render_a_board():
    body = render_board([], [], confession_rows=[_pending(1)])
    assert CONFESSION_HEADING in body
    assert body != EMPTY_BOARD


def test_the_confession_section_takes_a_bounded_slice():
    rows = [_pending(n, f"Confession {n}") for n in range(MAX_CONFESSION_ROWS + 4)]
    body = render_board([], [], confession_rows=rows, confession_total=len(rows))
    assert f"Confession {MAX_CONFESSION_ROWS}" not in body
    assert "**4** more" in body


def test_confessions_never_starve_the_task_list():
    body = render_board(
        [_chore(n, f"Chore {n}", todo_id=n) for n in range(MAX_CHORE_ROWS)],
        [_row(n, f"Task {n}") for n in range(MAX_BOARD_ROWS)],
        signoff_rows=[_claim(n) for n in range(MAX_SIGNOFF_ROWS + 5)],
        approval_rows=[_approval(n) for n in range(MAX_APPROVAL_ROWS + 5)],
        confession_rows=[_pending(n) for n in range(MAX_CONFESSION_ROWS + 5)],
    )
    for n in range(MIN_TASK_ROWS):
        assert f"Task {n}" in body


def test_the_confession_budget_comes_off_the_task_list():
    assert task_row_budget(0, confessions_shown=4) == MAX_BOARD_ROWS - 4


def test_the_footer_counts_confessions_to_approve():
    assert "1 confession to approve" in render_board_footer([], 3, 0, 0, 1)
    assert "2 confessions to approve" in render_board_footer([], 3, 0, 0, 2)


def test_the_footer_drops_the_confession_half_when_none_wait():
    assert "to approve" not in render_board_footer([], 3, 1, 1, 0)


# ── the confessions signature ─────────────────────────────────────────


def test_a_new_confession_repaints_the_board():
    before = board_content_signature([], [], 0, confession_rows=[], confession_total=0)
    after = board_content_signature(
        [], [], 0, confession_rows=[_pending(1)], confession_total=1
    )
    assert before != after


def test_a_resolved_confession_repaints_the_board():
    before = board_content_signature(
        [], [], 0, confession_rows=[_pending(1), _pending(2)], confession_total=2
    )
    after = board_content_signature(
        [], [], 0, confession_rows=[_pending(1)], confession_total=1
    )
    assert before != after


def test_a_confession_ageing_does_not_repaint_the_board():
    """``rel_ts`` re-renders in the client, so "2h" becoming "3h" must not
    spend an API call."""
    assert confession_signature([_pending(1, created_at=NOW)]) == confession_signature(
        [_pending(1, created_at=NOW - 90_000)]
    )
