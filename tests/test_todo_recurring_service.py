"""Tests for services/todo_recurring_service.py.

Recurring definitions are reminders: when one comes due it spawns an ordinary
todo row. The behaviour that matters is the *cadence* math and the
daily-reset rule, so everything here injects ``now_ts`` rather than sleeping.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.todo_recurring_service import (
    RecurringValidationError,
    create_recurring,
    delete_recurring,
    describe_cadence,
    get_recurring,
    chore_board_rows,
    chore_streaks,
    has_open_instance,
    list_recurring,
    normalize_days,
    run_now,
    set_status,
    spawn_due,
    update_recurring,
    validate,
)
from bot_modules.services.todo_service import (
    complete_todo,
    mark_missed,
    pending_todos,
)
from tests.db_template import migrated_db

GUILD = 123
USER = 9001

_EPOCH = datetime(1970, 1, 1)


def _epoch(y: int, mo: int, d: int, hh: int = 0, mm: int = 0) -> float:
    """UTC epoch for a wall-clock instant (guild offset 0 in most tests)."""
    return (datetime(y, mo, d, hh, mm) - _EPOCH).total_seconds()


def _zero_offset(_guild_id: int) -> float:
    return 0.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


# ── validation ────────────────────────────────────────────────────────


def test_validate_normalizes_daily_fields():
    task, recurrence, minutes, days = validate(
        task="  Post QOTD  ", recurrence="daily", time_of_day=540, recur_days=[1, 2]
    )
    assert task == "Post QOTD"
    assert recurrence == "daily"
    assert minutes == 540
    # A daily entry's weekday set is meaningless — it must not be persisted.
    assert days == ()


def test_validate_sorts_and_dedups_weekly_days():
    _, _, _, days = validate(
        task="x", recurrence="weekly", time_of_day=0, recur_days=[3, 0, 3]
    )
    assert days == (0, 3)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (dict(task="   ", recurrence="daily", time_of_day=0, recur_days=None), "empty"),
        (dict(task="x" * 501, recurrence="daily", time_of_day=0, recur_days=None), "500"),
        (dict(task="x", recurrence="hourly", time_of_day=0, recur_days=None), "daily or weekly"),
        (dict(task="x", recurrence="daily", time_of_day=-1, recur_days=None), "00:00"),
        (dict(task="x", recurrence="daily", time_of_day=1440, recur_days=None), "00:00"),
        (dict(task="x", recurrence="daily", time_of_day="noon", recur_days=None), "number"),
        (dict(task="x", recurrence="weekly", time_of_day=0, recur_days=[]), "day of the week"),
    ],
)
def test_validate_rejects(kwargs, message):
    with pytest.raises(RecurringValidationError) as err:
        validate(**kwargs)
    assert message in str(err.value)


def test_normalize_days_drops_out_of_range_and_junk():
    assert normalize_days([0, 7, -1, "3", None]) == (0, 3)


# ── create / list / update / delete ───────────────────────────────────


def test_create_computes_first_next_run(db):
    now = _epoch(2026, 7, 26, 8, 0)  # 08:00
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, created_by=USER, now_ts=now,  # 09:00
        )
        task = get_recurring(conn, rid, GUILD)
    assert task is not None
    assert task.next_run_at == _epoch(2026, 7, 26, 9, 0)
    assert task.status == "active"


def test_create_past_time_today_rolls_to_tomorrow(db):
    now = _epoch(2026, 7, 26, 10, 0)  # already past 09:00
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        task = get_recurring(conn, rid, GUILD)
    assert task.next_run_at == _epoch(2026, 7, 27, 9, 0)


def test_create_weekly_picks_next_matching_weekday(db):
    # 2026-07-26 is a Sunday (weekday 6); ask for Monday (0).
    now = _epoch(2026, 7, 26, 12, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Photo challenge prompt", recurrence="weekly",
            time_of_day=600, recur_days=[0], now_ts=now,
        )
        task = get_recurring(conn, rid, GUILD)
    assert task.next_run_at == _epoch(2026, 7, 27, 10, 0)
    assert task.recur_days == (0,)


def test_create_honours_guild_offset(db):
    """time_of_day is guild-local; a +2 guild fires two hours earlier in UTC."""
    now = _epoch(2026, 7, 26, 0, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, offset_hours=2.0, now_ts=now,
        )
        task = get_recurring(conn, rid, GUILD)
    assert task.next_run_at == _epoch(2026, 7, 26, 7, 0)


def test_list_is_scoped_by_guild(db):
    now = _epoch(2026, 7, 26)
    with open_db(db) as conn:
        create_recurring(conn, GUILD, task="Mine", recurrence="daily", time_of_day=0, now_ts=now)
        create_recurring(conn, 999, task="Theirs", recurrence="daily", time_of_day=0, now_ts=now)
        mine = list_recurring(conn, GUILD)
    assert [t.task for t in mine] == ["Mine"]


def test_update_rewrites_cadence_and_recomputes(db):
    now = _epoch(2026, 7, 26, 8, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily", time_of_day=540, now_ts=now
        )
        assert update_recurring(
            conn, rid, GUILD, task="Post QOTD (new)", recurrence="weekly",
            time_of_day=600, recur_days=[0], now_ts=now,
        )
        task = get_recurring(conn, rid, GUILD)
    assert task.task == "Post QOTD (new)"
    assert task.recurrence == "weekly"
    assert task.next_run_at == _epoch(2026, 7, 27, 10, 0)


def test_update_and_delete_reject_other_guilds(db):
    now = _epoch(2026, 7, 26)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Mine", recurrence="daily", time_of_day=0, now_ts=now
        )
        assert not update_recurring(
            conn, rid, 999, task="Hijacked", recurrence="daily",
            time_of_day=0, now_ts=now,
        )
        assert not delete_recurring(conn, rid, 999)
        assert get_recurring(conn, rid, GUILD).task == "Mine"


def test_delete_leaves_already_spawned_rows(db):
    """Spawned rows are real outstanding work — deleting the definition must not
    silently remove a task a mod is part-way through."""
    now = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now - 60,
        )
        spawn_due(conn, now_ts=now, offset_hours_for=_zero_offset)
        assert len(pending_todos(conn, GUILD)) == 1
        assert delete_recurring(conn, rid, GUILD)
        assert len(pending_todos(conn, GUILD)) == 1


# ── pause / resume ────────────────────────────────────────────────────


def test_paused_definition_does_not_spawn(db):
    now = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now - 60,
        )
        set_status(conn, rid, GUILD, "paused", now_ts=now)
        assert spawn_due(conn, now_ts=now, offset_hours_for=_zero_offset) == []
        assert pending_todos(conn, GUILD) == []


def test_resume_recomputes_from_now_not_the_stale_slot(db):
    """A long pause must not come back and immediately fire an old slot."""
    created = _epoch(2026, 7, 26, 8, 0)
    resumed = _epoch(2026, 8, 1, 8, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=created,
        )
        set_status(conn, rid, GUILD, "paused", now_ts=created)
        set_status(conn, rid, GUILD, "active", now_ts=resumed)
        task = get_recurring(conn, rid, GUILD)
    assert task.status == "active"
    assert task.next_run_at == _epoch(2026, 8, 1, 9, 0)


def test_set_status_rejects_unknown_status(db):
    now = _epoch(2026, 7, 26)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="x", recurrence="daily", time_of_day=0, now_ts=now
        )
        with pytest.raises(RecurringValidationError):
            set_status(conn, rid, GUILD, "cancelled", now_ts=now)


# ── the tick ──────────────────────────────────────────────────────────


def test_spawn_creates_todo_and_advances(db):
    due = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, description="Use the sponsor queue if there is one.",
            now_ts=due - 60,
        )
        results = spawn_due(conn, now_ts=due, offset_hours_for=_zero_offset)
        rows = pending_todos(conn, GUILD)
        task = get_recurring(conn, rid, GUILD)

    assert [r.status for r in results] == ["spawned"]
    assert len(rows) == 1
    assert rows[0]["task"] == "Post QOTD"
    assert rows[0]["recurring_id"] == rid
    assert rows[0]["description"] == "Use the sponsor queue if there is one."
    assert task.next_run_at == _epoch(2026, 7, 27, 9, 0)
    assert task.last_status == "spawned"


def test_scheduled_fire_writes_off_the_undone_instance_and_spawns_fresh(db):
    """The daily reset, which replaced skip-if-pending.

    Midnight is a day boundary, not a reason to keep yesterday's row: the
    untouched instance is written off and today gets its own. Exactly one row is
    outstanding either way — nothing stacks — but the board can now say *which
    day* it is asking about, and one tick can no longer credit two days.
    """
    day1 = _epoch(2026, 7, 26, 9, 0)
    day2 = _epoch(2026, 7, 27, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=day1 - 60,
        )
        spawn_due(conn, now_ts=day1, offset_hours_for=_zero_offset)
        yesterday = pending_todos(conn, GUILD)[0]["id"]
        results = spawn_due(conn, now_ts=day2, offset_hours_for=_zero_offset)
        rows = pending_todos(conn, GUILD)
        task = get_recurring(conn, rid, GUILD)
        written_off = conn.execute(
            "SELECT missed_at, completed_at FROM todos WHERE id = ?", (yesterday,)
        ).fetchone()

    assert [r.status for r in results] == ["spawned"]
    assert [r.missed_todo_id for r in results] == [yesterday]
    # Still exactly one outstanding row — but a *new* one.
    assert len(rows) == 1
    assert rows[0]["id"] != yesterday
    assert rows[0]["created_at"] == day2
    # Yesterday is closed, and closed without being credited to anyone.
    assert written_off["missed_at"] == day2
    assert written_off["completed_at"] is None
    assert task.last_status == "spawned"
    assert task.next_run_at == _epoch(2026, 7, 28, 9, 0)


def test_a_written_off_day_survives_as_a_record(db):
    """The reset's whole payoff: the days it did not happen are still there.

    Under skip-if-pending a skipped day left no trace at all — the same single
    row simply aged — so "we missed Monday and Tuesday" was unanswerable and no
    streak could be computed. Three unattended days must leave three rows.
    """
    days = [_epoch(2026, 7, 26 + n, 9, 0) for n in range(4)]
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=days[0] - 60,
        )
        for day in days:
            spawn_due(conn, now_ts=day, offset_hours_for=_zero_offset)
        missed = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE missed_at IS NOT NULL"
        ).fetchone()[0]
        assert missed == 3  # the first three days; the fourth is still open
        assert len(pending_todos(conn, GUILD)) == 1


def test_a_written_off_row_cannot_be_ticked_later(db):
    """Yesterday's box is not tickable today.

    Crediting a written-off row would invent a completion that never happened
    and put a hole in the middle of the streak either side of it.
    """
    day1 = _epoch(2026, 7, 26, 9, 0)
    day2 = _epoch(2026, 7, 27, 9, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=day1 - 60,
        )
        spawn_due(conn, now_ts=day1, offset_hours_for=_zero_offset)
        yesterday = pending_todos(conn, GUILD)[0]["id"]
        spawn_due(conn, now_ts=day2, offset_hours_for=_zero_offset)

        assert complete_todo(conn, yesterday, GUILD, USER, now_ts=day2 + 60) is False
        row = conn.execute(
            "SELECT completed_at, completed_by FROM todos WHERE id = ?", (yesterday,)
        ).fetchone()
    assert row["completed_at"] is None
    assert not row["completed_by"]


def test_a_written_off_row_leaves_the_all_todos_board(db):
    """The reset also has to clear the *other* board.

    A missed chore is closed. If ``pending_todos`` still returned it, the
    all-todos board would carry a growing pile of dead chore rows nobody can
    tick — the exact stacking the old rule existed to prevent, reintroduced by
    the fix for it.
    """
    day1 = _epoch(2026, 7, 26, 9, 0)
    day2 = _epoch(2026, 7, 27, 9, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=day1 - 60,
        )
        spawn_due(conn, now_ts=day1, offset_hours_for=_zero_offset)
        spawn_due(conn, now_ts=day2, offset_hours_for=_zero_offset)
        rows = pending_todos(conn, GUILD)
    assert len(rows) == 1
    assert rows[0]["created_at"] == day2


def test_reset_does_not_write_off_a_completed_instance(db):
    """A ticked chore is already closed — the reset must not touch it."""
    day1 = _epoch(2026, 7, 26, 9, 0)
    day2 = _epoch(2026, 7, 27, 9, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=day1 - 60,
        )
        spawn_due(conn, now_ts=day1, offset_hours_for=_zero_offset)
        done = pending_todos(conn, GUILD)[0]["id"]
        complete_todo(conn, done, GUILD, USER, now_ts=day1 + 60)
        results = spawn_due(conn, now_ts=day2, offset_hours_for=_zero_offset)
        row = conn.execute(
            "SELECT missed_at FROM todos WHERE id = ?", (done,)
        ).fetchone()

    assert [r.missed_todo_id for r in results] == [None]
    assert row["missed_at"] is None


def test_spawn_resumes_after_the_instance_is_completed(db):
    day1 = _epoch(2026, 7, 26, 9, 0)
    day2 = _epoch(2026, 7, 27, 9, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=day1 - 60,
        )
        spawn_due(conn, now_ts=day1, offset_hours_for=_zero_offset)
        first = pending_todos(conn, GUILD)[0]
        assert complete_todo(conn, first["id"], GUILD, USER, now_ts=day1 + 100)
        results = spawn_due(conn, now_ts=day2, offset_hours_for=_zero_offset)
        rows = pending_todos(conn, GUILD)

    assert [r.status for r in results] == ["spawned"]
    assert len(rows) == 1
    assert rows[0]["id"] != first["id"]


def test_spawn_catches_up_only_once_after_downtime(db):
    """Three days offline spawns one row on boot, not three."""
    created = _epoch(2026, 7, 26, 8, 0)
    back_online = _epoch(2026, 7, 29, 12, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=created,
        )
        results = spawn_due(conn, now_ts=back_online, offset_hours_for=_zero_offset)
        rows = pending_todos(conn, GUILD)
        task = get_recurring(conn, rid, GUILD)

    assert [r.status for r in results] == ["spawned"]
    assert len(rows) == 1
    # Advanced past every missed slot to the next future one.
    assert task.next_run_at == _epoch(2026, 7, 30, 9, 0)


def test_spawn_ignores_not_yet_due(db):
    now = _epoch(2026, 7, 26, 8, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        assert spawn_due(conn, now_ts=now, offset_hours_for=_zero_offset) == []
        assert pending_todos(conn, GUILD) == []


def test_spawn_weekly_only_on_selected_days(db):
    """A Monday-only entry does not fire on Tuesday."""
    sunday = _epoch(2026, 7, 26, 12, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Photo challenge prompt", recurrence="weekly",
            time_of_day=600, recur_days=[0], now_ts=sunday,
        )
        # Monday 10:00 → fires.
        assert len(spawn_due(conn, now_ts=_epoch(2026, 7, 27, 10, 0),
                             offset_hours_for=_zero_offset)) == 1
        row = pending_todos(conn, GUILD)[0]
        complete_todo(conn, row["id"], GUILD, USER, now_ts=_epoch(2026, 7, 27, 11, 0))
        # Tuesday 10:00 → not a selected day, nothing due.
        assert spawn_due(conn, now_ts=_epoch(2026, 7, 28, 10, 0),
                         offset_hours_for=_zero_offset) == []


def test_spawn_uses_per_guild_offset(db):
    """Each guild's local 09:00 is a different UTC instant."""
    now = _epoch(2026, 7, 26, 7, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="A", recurrence="daily", time_of_day=540,
            offset_hours=2.0, now_ts=now - 60,
        )
        create_recurring(
            conn, 999, task="B", recurrence="daily", time_of_day=540,
            offset_hours=0.0, now_ts=now - 60,
        )
        results = spawn_due(
            conn, now_ts=now, offset_hours_for=lambda gid: 2.0 if gid == GUILD else 0.0
        )
    # 07:00 UTC is 09:00 in the +2 guild but only 07:00 in the +0 guild.
    assert [r.guild_id for r in results] == [GUILD]


def test_spawned_rows_are_isolated_by_guild(db):
    due = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="A", recurrence="daily", time_of_day=540, now_ts=due - 60
        )
        create_recurring(
            conn, 999, task="B", recurrence="daily", time_of_day=540, now_ts=due - 60
        )
        spawn_due(conn, now_ts=due, offset_hours_for=_zero_offset)
        assert [r["task"] for r in pending_todos(conn, GUILD)] == ["A"]
        assert [r["task"] for r in pending_todos(conn, 999)] == ["B"]


def test_has_open_instance_tracks_completion(db):
    due = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=due - 60,
        )
        assert not has_open_instance(conn, rid)
        spawn_due(conn, now_ts=due, offset_hours_for=_zero_offset)
        assert has_open_instance(conn, rid)
        row = pending_todos(conn, GUILD)[0]
        complete_todo(conn, row["id"], GUILD, USER, now_ts=due + 10)
        assert not has_open_instance(conn, rid)


# ── run now ───────────────────────────────────────────────────────────


def test_run_now_spawns_immediately(db):
    now = _epoch(2026, 7, 26, 8, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        result = run_now(conn, rid, GUILD, now_ts=now)
        rows = pending_todos(conn, GUILD)
        task = get_recurring(conn, rid, GUILD)

    assert result is not None and result.status == "spawned"
    assert len(rows) == 1
    # The configured schedule is untouched — "add one now" is not "reschedule".
    assert task.next_run_at == _epoch(2026, 7, 26, 9, 0)
    assert task.last_status == "spawned"


def test_run_now_respects_skip_if_pending(db):
    now = _epoch(2026, 7, 26, 8, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        run_now(conn, rid, GUILD, now_ts=now)
        result = run_now(conn, rid, GUILD, now_ts=now + 60)
        assert result is not None and result.status == "skipped_pending"
        assert len(pending_todos(conn, GUILD)) == 1


def test_run_now_leaves_a_paused_entry_paused(db):
    """Regression: "Run now" used to force status='active'. A mod who paused
    an entry for the holidays and then added one instance by hand would have
    silently resumed the daily schedule."""
    now = _epoch(2026, 7, 26, 8, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        set_status(conn, rid, GUILD, "paused", now_ts=now)
        result = run_now(conn, rid, GUILD, now_ts=now)
        assert result is not None and result.status == "spawned"
        assert len(pending_todos(conn, GUILD)) == 1
        assert get_recurring(conn, rid, GUILD).status == "paused"


def test_run_now_does_not_touch_other_guilds(db):
    """Regression: run_now delegated to the guild-blind spawn_due with the
    *requesting* guild's UTC offset, so one guild's button press spawned other
    guilds' due chores and rewrote their next_run_at to the wrong wall clock."""
    now = _epoch(2026, 7, 26, 12, 0)
    with open_db(db) as conn:
        mine = create_recurring(
            conn, GUILD, task="Mine", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        # Another guild with a chore that is already due, on a +10 offset.
        theirs = create_recurring(
            conn, 999, task="Theirs", recurrence="daily",
            time_of_day=540, offset_hours=10.0, now_ts=now - 86400,
        )
        before = get_recurring(conn, theirs, 999).next_run_at

        run_now(conn, mine, GUILD, now_ts=now, offset_hours=-5.0)

        assert [r["task"] for r in pending_todos(conn, GUILD)] == ["Mine"]
        assert pending_todos(conn, 999) == []
        assert get_recurring(conn, theirs, 999).next_run_at == before


def test_run_now_missing_returns_none(db):
    with open_db(db) as conn:
        assert run_now(conn, 4242, GUILD, now_ts=_epoch(2026, 7, 26)) is None


# ── rendering ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("recurrence", "minutes", "days", "expected"),
    [
        ("daily", 540, None, "Daily at 09:00"),
        ("daily", 0, None, "Daily at 00:00"),
        ("weekly", 1110, [0, 3], "Weekly on Mon, Thu at 18:30"),
        ("weekly", 600, [6], "Weekly on Sun at 10:00"),
    ],
)
def test_describe_cadence(db, recurrence, minutes, days, expected):
    now = _epoch(2026, 7, 26)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="x", recurrence=recurrence,
            time_of_day=minutes, recur_days=days, now_ts=now,
        )
        task = get_recurring(conn, rid, GUILD)
    assert describe_cadence(task) == expected


def test_concurrent_spawners_cannot_double_insert(db):
    """Skip-if-pending must hold across two writers, not just within one.

    The background loop and a dashboard "Run now" both read
    ``has_open_instance`` then write. On separate *deferred* transactions each
    can read the same "nothing open" snapshot and insert, so both spawn paths
    take the write lock up front (``open_db_immediate``). This test pins the
    invariant the locking exists to protect.
    """
    from bot_modules.core.db_utils import open_db_immediate

    now = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now - 60,
        )

    # Two serialized spawn attempts against the same due row.
    with open_db_immediate(db) as conn:
        first = spawn_due(conn, now_ts=now, offset_hours_for=_zero_offset)
    with open_db_immediate(db) as conn:
        second = spawn_due(conn, now_ts=now, offset_hours_for=_zero_offset)

    assert [r.status for r in first] == ["spawned"]
    # The second attempt finds next_run_at already advanced, so nothing is due.
    assert second == []
    with open_db(db) as conn:
        assert len(pending_todos(conn, GUILD)) == 1
        assert get_recurring(conn, rid, GUILD).next_run_at == _epoch(2026, 7, 27, 9, 0)


# ── "Run now" keeps skip-if-pending ─────────────────────────────────────


def test_run_now_never_writes_off_the_open_instance(db):
    """A double click is not a day boundary.

    ``run_now`` is a mod adding one more instance by hand. If it reset like a
    scheduled fire does, pressing the button twice would mark the first press
    missed — fabricating a failure out of a double click, and putting a break
    in a streak that nothing actually broke.
    """
    now = _epoch(2026, 7, 26, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now - 60,
        )
        first = run_now(conn, rid, GUILD, now_ts=now)
        second = run_now(conn, rid, GUILD, now_ts=now + 5)
        missed = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE missed_at IS NOT NULL"
        ).fetchone()[0]

    assert first is not None and first.status == "spawned"
    assert second is not None and second.status == "skipped_pending"
    assert second.missed_todo_id is None
    assert missed == 0


# ── streaks ─────────────────────────────────────────────────────────────


def _daily_chore(conn, *, start: float, task: str = "Post QOTD") -> int:
    return create_recurring(
        conn, GUILD, task=task, recurrence="daily",
        time_of_day=540, now_ts=start - 60,
    )


def _run_days(conn, outcomes: list[bool], start_day: int = 26) -> None:
    """Drive N daily occurrences, ticking the ones flagged True.

    Each occurrence spawns, and the previous one is written off by the reset
    unless it was ticked — which is exactly the history a streak reads.
    """
    for offset, did_it in enumerate(outcomes):
        day = _epoch(2026, 7, start_day + offset, 9, 0)
        spawn_due(conn, now_ts=day, offset_hours_for=_zero_offset)
        if did_it:
            row = pending_todos(conn, GUILD)[0]
            complete_todo(conn, row["id"], GUILD, USER, now_ts=day + 60)


def test_streak_counts_consecutive_completed_days(db):
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [True, True, True])
        assert chore_streaks(conn, GUILD) == {rid: 3}


def test_streak_breaks_at_a_missed_day(db):
    """Only the run since the last miss counts — that is what a streak is."""
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [True, True, False, True, True])
        assert chore_streaks(conn, GUILD) == {rid: 2}


def test_todays_open_row_does_not_zero_the_streak(db):
    """A chore due at 09:00 must not read as a broken streak all morning.

    The newest instance is still outstanding — the day is undecided, not
    failed. Counting it as a break would show 🔥 0 on every chore every day
    until someone ticked it, which makes the number worthless.
    """
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [True, True])
        # One more occurrence, left untouched: today.
        spawn_due(conn, now_ts=_epoch(2026, 7, 28, 9, 0), offset_hours_for=_zero_offset)
        assert chore_streaks(conn, GUILD) == {rid: 2}


def test_streak_is_zero_before_anything_is_ticked(db):
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [False, False])
        assert chore_streaks(conn, GUILD) == {rid: 0}


def test_streaks_are_per_definition(db):
    with open_db(db) as conn:
        good = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0), task="QOTD")
        bad = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0), task="Mod queue")
        for offset in range(3):
            day = _epoch(2026, 7, 26 + offset, 9, 0)
            spawn_due(conn, now_ts=day, offset_hours_for=_zero_offset)
            for row in pending_todos(conn, GUILD):
                if row["task"] == "QOTD":
                    complete_todo(conn, row["id"], GUILD, USER, now_ts=day + 60)
        streaks = chore_streaks(conn, GUILD)
    assert streaks[good] == 3
    assert streaks[bad] == 0


def test_streak_lookback_bounds_the_walk(db):
    """The walk is capped, so a chore with years of history stays cheap."""
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [True] * 5)
        assert chore_streaks(conn, GUILD, lookback=2) == {rid: 2}


# ── the chore board's rows ──────────────────────────────────────────────


def test_chore_board_rows_carry_state_and_streak(db):
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [True, True])
        rows = chore_board_rows(conn, GUILD)

    assert len(rows) == 1
    row = rows[0]
    assert row["recurring_id"] == rid
    assert row["task"] == "Post QOTD"
    assert row["completed_at"] is not None
    assert row["completed_by"] == USER
    assert row["streak"] == 2


def test_chore_board_shows_the_latest_instance_only(db):
    """A scoreboard shows today, not the whole history."""
    with open_db(db) as conn:
        _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        _run_days(conn, [True, False, True])
        rows = chore_board_rows(conn, GUILD)

    assert len(rows) == 1
    assert rows[0]["completed_at"] is not None  # the most recent day, ticked
    assert rows[0]["missed_at"] is None


def test_chore_board_shows_a_missed_chore_as_missed(db):
    with open_db(db) as conn:
        _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        spawn_due(conn, now_ts=_epoch(2026, 7, 26, 9, 0), offset_hours_for=_zero_offset)
        open_id = pending_todos(conn, GUILD)[0]["id"]
        mark_missed(conn, open_id, now_ts=_epoch(2026, 7, 27, 9, 0))
        rows = chore_board_rows(conn, GUILD)

    assert len(rows) == 1
    assert rows[0]["missed_at"] is not None
    assert rows[0]["completed_at"] is None


def test_chore_board_omits_paused_definitions(db):
    """A chore paused for the holidays is not a chore the team is failing."""
    with open_db(db) as conn:
        live = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0), task="QOTD")
        parked = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0), task="Newsletter")
        set_status(conn, parked, GUILD, "paused", now_ts=_epoch(2026, 7, 26, 9, 0))
        rows = chore_board_rows(conn, GUILD)

    assert [r["recurring_id"] for r in rows] == [live]


def test_chore_board_includes_a_definition_that_has_never_run(db):
    """Configured but not yet due still belongs on the board, as not-done."""
    with open_db(db) as conn:
        rid = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0))
        rows = chore_board_rows(conn, GUILD)

    assert [r["recurring_id"] for r in rows] == [rid]
    assert rows[0]["todo_id"] is None
    assert rows[0]["streak"] == 0


def test_chore_board_reads_in_time_of_day_order(db):
    """The board scans like a shift checklist, not by id."""
    now = _epoch(2026, 7, 26, 0, 0)
    with open_db(db) as conn:
        evening = create_recurring(
            conn, GUILD, task="Evening sweep", recurrence="daily",
            time_of_day=1200, now_ts=now,
        )
        morning = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=now,
        )
        rows = chore_board_rows(conn, GUILD)

    assert [r["recurring_id"] for r in rows] == [morning, evening]


def test_chore_board_is_scoped_to_its_guild(db):
    with open_db(db) as conn:
        mine = _daily_chore(conn, start=_epoch(2026, 7, 26, 9, 0), task="Mine")
        create_recurring(
            conn, 999, task="Theirs", recurrence="daily",
            time_of_day=540, now_ts=_epoch(2026, 7, 26, 9, 0),
        )
        rows = chore_board_rows(conn, GUILD)

    assert [r["recurring_id"] for r in rows] == [mine]
