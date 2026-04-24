"""Tests for services/watch_service.py."""

from __future__ import annotations

import pytest

from db_utils import open_db
from migrations import apply_migrations_sync
from services.watch_service import add_watched_user, load_watched_users, remove_watched_user

GUILD = 123
WATCHER = 9001
WATCHED_A = 1001
WATCHED_B = 1002


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


# ── add_watched_user ──────────────────────────────────────────────────


def test_add_returns_true_on_new_entry(db):
    with open_db(db) as conn:
        assert add_watched_user(conn, GUILD, WATCHED_A, WATCHER) is True


def test_add_idempotent_returns_false(db):
    with open_db(db) as conn:
        add_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        assert add_watched_user(conn, GUILD, WATCHED_A, WATCHER) is False


def test_add_multiple_watchers_for_same_user(db):
    watcher2 = 9002
    with open_db(db) as conn:
        add_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        add_watched_user(conn, GUILD, WATCHED_A, watcher2)
        result = load_watched_users(conn, GUILD)
    assert result[WATCHED_A] == {WATCHER, watcher2}


# ── remove_watched_user ───────────────────────────────────────────────


def test_remove_returns_true_when_deleted(db):
    with open_db(db) as conn:
        add_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        assert remove_watched_user(conn, GUILD, WATCHED_A, WATCHER) is True


def test_remove_returns_false_when_missing(db):
    with open_db(db) as conn:
        assert remove_watched_user(conn, GUILD, WATCHED_A, WATCHER) is False


def test_remove_only_affects_target_pair(db):
    watcher2 = 9002
    with open_db(db) as conn:
        add_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        add_watched_user(conn, GUILD, WATCHED_A, watcher2)
        remove_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        result = load_watched_users(conn, GUILD)
    assert result[WATCHED_A] == {watcher2}


# ── load_watched_users ────────────────────────────────────────────────


def test_load_empty_guild(db):
    with open_db(db) as conn:
        assert load_watched_users(conn, GUILD) == {}


def test_load_returns_correct_structure(db):
    with open_db(db) as conn:
        add_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        add_watched_user(conn, GUILD, WATCHED_B, WATCHER)
        result = load_watched_users(conn, GUILD)
    assert WATCHED_A in result
    assert WATCHED_B in result
    assert WATCHER in result[WATCHED_A]
    assert WATCHER in result[WATCHED_B]


def test_load_isolated_by_guild(db):
    other_guild = 999
    with open_db(db) as conn:
        add_watched_user(conn, GUILD, WATCHED_A, WATCHER)
        add_watched_user(conn, other_guild, WATCHED_B, WATCHER)
        assert WATCHED_B not in load_watched_users(conn, GUILD)
        assert WATCHED_A not in load_watched_users(conn, other_guild)
