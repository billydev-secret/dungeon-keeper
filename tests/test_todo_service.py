"""Tests for services/todo_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.todo_service import create_todo
from migrations import apply_migrations_sync

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
        row = conn.execute(
            "SELECT guild_id, added_by, task, completed_at FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["guild_id"] == GUILD
    assert row["added_by"] == USER
    assert row["task"] == "Do the thing"
    assert row["completed_at"] is None


def test_create_isolated_by_guild(db):
    other_guild = 999
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Guild A task")
        create_todo(conn, other_guild, USER, "Guild B task")
        a_count = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
        b_count = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE guild_id = ?", (other_guild,)
        ).fetchone()[0]
    assert a_count == 1
    assert b_count == 1


# ── create_todo with new optional fields ─────────────────────────────


def test_create_with_description_and_source_url(db):
    with open_db(db) as conn:
        todo_id = create_todo(
            conn,
            GUILD,
            USER,
            "Message from @alice in #general",
            description="hello world\n\nfollow up next week",
            source_message_url="https://discord.com/channels/1/2/3",
        )
        row = conn.execute(
            "SELECT description, source_message_url FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["description"] == "hello world\n\nfollow up next week"
    assert row["source_message_url"] == "https://discord.com/channels/1/2/3"


def test_create_without_new_fields_leaves_them_null(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Plain task")
        row = conn.execute(
            "SELECT description, source_message_url FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["description"] is None
    assert row["source_message_url"] is None
