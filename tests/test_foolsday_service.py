"""Tests for services/foolsday_service.py."""

from __future__ import annotations

import time

import pytest

from db_utils import open_db
from migrations import apply_migrations_sync
from services.foolsday_service import (
    DAY_SECONDS,
    active_user_ids,
    add_exclusion,
    clear_names,
    derangement,
    excluded_user_ids,
    init_tables,
    load_names,
    remove_exclusion,
    save_names,
)

GUILD = 123


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        init_tables(conn)
    return db_path


# ── derangement ───────────────────────────────────────────────────────


def test_derangement_single_item():
    assert derangement(["Alice"], ["Alice"]) == ["Alice"]


def test_derangement_no_fixed_points():
    items = ["Alice", "Bob", "Carol", "Dan"]
    own = ["Alice", "Bob", "Carol", "Dan"]
    result = derangement(items, own)
    assert len(result) == len(items)
    assert set(result) == set(items)
    assert all(result[i] != own[i] for i in range(len(items)))


def test_derangement_two_items():
    result = derangement(["Alice", "Bob"], ["Alice", "Bob"])
    assert result == ["Bob", "Alice"]


def test_derangement_impossible_falls_back():
    # All same name — can't derange, just returns some permutation
    result = derangement(["Alice", "Alice", "Alice"], ["Alice", "Alice", "Alice"])
    assert len(result) == 3
    assert set(result) == {"Alice"}


def test_derangement_preserves_pool():
    items = ["Alice", "Bob", "Carol"]
    own = ["Alice", "Bob", "Carol"]
    result = derangement(items, own)
    assert sorted(result) == sorted(items)


def test_derangement_best_effort_swap_path():
    # Force fallback swap path: 3 items, 2 identical names — perfect derangement
    # may be impossible for one slot (only "Alice" available to swap into it).
    # seed=0 ensures deterministic shuffle hits the fallback branch.
    import random as _rand
    _rand.seed(42)
    items = ["Alice", "Alice", "Bob"]
    own = ["Alice", "Alice", "Bob"]
    result = derangement(items, own)
    assert len(result) == 3
    assert sorted(result) == sorted(items)


# ── init_tables / save_names / load_names / clear_names ──────────────


def test_save_and_load_names(db):
    names = {1001: "Alice", 1002: "Bob"}
    with open_db(db) as conn:
        save_names(conn, GUILD, names)
        loaded = load_names(conn, GUILD)
    assert loaded == names


def test_save_names_overwrites_previous(db):
    with open_db(db) as conn:
        save_names(conn, GUILD, {1001: "Alice", 1002: "Bob"})
        save_names(conn, GUILD, {1003: "Carol"})
        loaded = load_names(conn, GUILD)
    assert loaded == {1003: "Carol"}


def test_load_names_empty(db):
    with open_db(db) as conn:
        assert load_names(conn, GUILD) == {}


def test_clear_names(db):
    with open_db(db) as conn:
        save_names(conn, GUILD, {1001: "Alice"})
        clear_names(conn, GUILD)
        assert load_names(conn, GUILD) == {}


def test_names_isolated_by_guild(db):
    other = 999
    with open_db(db) as conn:
        save_names(conn, GUILD, {1001: "Alice"})
        save_names(conn, other, {1002: "Bob"})
        assert load_names(conn, GUILD) == {1001: "Alice"}
        assert load_names(conn, other) == {1002: "Bob"}


# ── exclusions ────────────────────────────────────────────────────────


def test_add_and_list_exclusions(db):
    with open_db(db) as conn:
        add_exclusion(conn, GUILD, 1001)
        add_exclusion(conn, GUILD, 1002)
        result = excluded_user_ids(conn, GUILD)
    assert result == {1001, 1002}


def test_add_exclusion_idempotent(db):
    with open_db(db) as conn:
        add_exclusion(conn, GUILD, 1001)
        add_exclusion(conn, GUILD, 1001)
        assert excluded_user_ids(conn, GUILD) == {1001}


def test_remove_exclusion_returns_true(db):
    with open_db(db) as conn:
        add_exclusion(conn, GUILD, 1001)
        assert remove_exclusion(conn, GUILD, 1001) is True
        assert excluded_user_ids(conn, GUILD) == set()


def test_remove_exclusion_missing_returns_false(db):
    with open_db(db) as conn:
        assert remove_exclusion(conn, GUILD, 9999) is False


def test_exclusions_isolated_by_guild(db):
    other = 999
    with open_db(db) as conn:
        add_exclusion(conn, GUILD, 1001)
        assert excluded_user_ids(conn, other) == set()


# ── active_user_ids ───────────────────────────────────────────────────


def _insert_processed_msg(conn, guild_id, user_id, created_at):
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages "
        "(guild_id, channel_id, message_id, user_id, created_at, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, 100, user_id * 1000 + int(created_at), user_id, created_at, created_at),
    )


def test_active_user_ids_enough_days(db):
    now = time.time()
    with open_db(db) as conn:
        for day_offset in range(3):
            _insert_processed_msg(conn, GUILD, 1001, now - day_offset * DAY_SECONDS - 1)
        result = active_user_ids(conn, GUILD, min_days=3, window_days=5)
    assert 1001 in result


def test_active_user_ids_not_enough_days(db):
    now = time.time()
    with open_db(db) as conn:
        # Only 2 distinct days, need 3
        for day_offset in range(2):
            _insert_processed_msg(conn, GUILD, 1001, now - day_offset * DAY_SECONDS - 1)
        result = active_user_ids(conn, GUILD, min_days=3, window_days=5)
    assert 1001 not in result


def test_active_user_ids_outside_window(db):
    now = time.time()
    with open_db(db) as conn:
        # Messages from 10 days ago — outside 5-day window
        for day_offset in range(3):
            _insert_processed_msg(conn, GUILD, 1001, now - (10 + day_offset) * DAY_SECONDS)
        result = active_user_ids(conn, GUILD, min_days=3, window_days=5)
    assert 1001 not in result


def test_active_user_ids_multiple_per_day_counts_once(db):
    now = time.time()
    with open_db(db) as conn:
        # 5 messages on same day — still counts as 1 day
        for i in range(5):
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages "
                "(guild_id, channel_id, message_id, user_id, created_at, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (GUILD, 100, 5000 + i, 1001, now - 100, now - 100),
            )
        result = active_user_ids(conn, GUILD, min_days=3, window_days=5)
    assert 1001 not in result
