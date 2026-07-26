"""Tests for services/todo_recurring_service.py.

Recurring definitions are reminders: when one comes due it spawns an ordinary
todo row. The behaviour that matters is the *cadence* math and the
skip-if-pending rule, so everything here injects ``now_ts`` rather than sleeping.
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
    has_open_instance,
    list_recurring,
    normalize_days,
    run_now,
    set_status,
    spawn_due,
    update_recurring,
    validate,
)
from bot_modules.services.todo_service import complete_todo, pending_todos
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


def test_spawn_skips_when_previous_instance_still_pending(db):
    """Skip-if-pending: a chore nobody did all week must not stack five rows."""
    day1 = _epoch(2026, 7, 26, 9, 0)
    day2 = _epoch(2026, 7, 27, 9, 0)
    with open_db(db) as conn:
        rid = create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=540, now_ts=day1 - 60,
        )
        spawn_due(conn, now_ts=day1, offset_hours_for=_zero_offset)
        results = spawn_due(conn, now_ts=day2, offset_hours_for=_zero_offset)
        rows = pending_todos(conn, GUILD)
        task = get_recurring(conn, rid, GUILD)

    assert [r.status for r in results] == ["skipped_pending"]
    assert len(rows) == 1  # still just the one, now a day old
    assert task.last_status == "skipped_pending"
    # The definition still advances, so it retries tomorrow.
    assert task.next_run_at == _epoch(2026, 7, 28, 9, 0)


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
