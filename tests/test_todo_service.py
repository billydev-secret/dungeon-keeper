"""Tests for services/todo_service.py."""

from __future__ import annotations

import time

import pytest

from db_utils import open_db
from migrations import apply_migrations_sync
from services.todo_service import complete_todo, create_todo, delete_todo, list_todos

GUILD = 123
USER = 9001


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


# ── create_todo ───────────────────────────────────────────────────────


def test_create_returns_id(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Fix the bug")
    assert isinstance(todo_id, int)
    assert todo_id > 0


def test_create_increments_ids(db):
    with open_db(db) as conn:
        id1 = create_todo(conn, GUILD, USER, "Task 1")
        id2 = create_todo(conn, GUILD, USER, "Task 2")
    assert id2 > id1


def test_create_stores_correct_values(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do the thing")
        todos = list_todos(conn, GUILD)
    assert len(todos) == 1
    t = todos[0]
    assert t["id"] == todo_id
    assert t["guild_id"] == GUILD
    assert t["added_by"] == USER
    assert t["task"] == "Do the thing"
    assert t["completed_at"] is None


# ── list_todos ────────────────────────────────────────────────────────


def test_list_empty_guild(db):
    with open_db(db) as conn:
        assert list_todos(conn, GUILD) == []


def test_list_excludes_completed_by_default(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Done task")
        complete_todo(conn, GUILD, todo_id)
        active = list_todos(conn, GUILD)
    assert active == []


def test_list_includes_completed_when_requested(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Done task")
        complete_todo(conn, GUILD, todo_id)
        all_todos = list_todos(conn, GUILD, include_completed=True)
    assert len(all_todos) == 1


def test_list_isolated_by_guild(db):
    other_guild = 999
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Guild A task")
        create_todo(conn, other_guild, USER, "Guild B task")
        assert len(list_todos(conn, GUILD)) == 1
        assert len(list_todos(conn, other_guild)) == 1


# ── complete_todo ─────────────────────────────────────────────────────


def test_complete_returns_true(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        assert complete_todo(conn, GUILD, todo_id) is True


def test_complete_sets_completed_at(db):
    before = time.time()
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        complete_todo(conn, GUILD, todo_id)
        todos = list_todos(conn, GUILD, include_completed=True)
    assert todos[0]["completed_at"] >= before


def test_complete_twice_returns_false(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        complete_todo(conn, GUILD, todo_id)
        assert complete_todo(conn, GUILD, todo_id) is False


def test_complete_wrong_guild_returns_false(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        assert complete_todo(conn, 999, todo_id) is False


# ── delete_todo ───────────────────────────────────────────────────────


def test_delete_returns_true(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        assert delete_todo(conn, GUILD, todo_id) is True


def test_delete_removes_todo(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        delete_todo(conn, GUILD, todo_id)
        assert list_todos(conn, GUILD, include_completed=True) == []


def test_delete_missing_returns_false(db):
    with open_db(db) as conn:
        assert delete_todo(conn, GUILD, 9999) is False


def test_delete_wrong_guild_returns_false(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Task")
        assert delete_todo(conn, 999, todo_id) is False
