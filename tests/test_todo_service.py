"""Tests for services/todo_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.todo_service import (
    clear_board,
    complete_todo,
    create_todo,
    get_board,
    guilds_with_board,
    list_todos,
    pending_count,
    pending_todos,
    save_board,
)
from tests.db_template import migrated_db

GUILD = 123
USER = 9001


def _read(conn, todo_id):
    """Read one row back. `get_todo` used to exist for this but had no
    production caller, so the test owns its own read-back."""
    return next(r for r in list_todos(conn, GUILD) if r["id"] == todo_id)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
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


# ── list / get / complete ─────────────────────────────────────────────


def test_list_returns_newest_first(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Older", now_ts=100.0)
        create_todo(conn, GUILD, USER, "Newer", now_ts=200.0)
        rows = list_todos(conn, GUILD)
    assert [r["task"] for r in rows] == ["Newer", "Older"]


def test_pending_returns_oldest_first(db):
    """The board reads oldest-first so the longest-waiting task nags at the top."""
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Older", now_ts=100.0)
        create_todo(conn, GUILD, USER, "Newer", now_ts=200.0)
        rows = pending_todos(conn, GUILD)
    assert [r["task"] for r in rows] == ["Older", "Newer"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("pending", ["Open"]), ("completed", ["Done"]), (None, ["Done", "Open"])],
)
def test_list_status_filter(db, status, expected):
    with open_db(db) as conn:
        open_id = create_todo(conn, GUILD, USER, "Open", now_ts=100.0)
        done_id = create_todo(conn, GUILD, USER, "Done", now_ts=200.0)
        complete_todo(conn, done_id, GUILD, USER, now_ts=300.0)
        rows = list_todos(conn, GUILD, status=status)
    assert [r["task"] for r in rows] == expected
    assert open_id != done_id


def test_list_is_scoped_by_guild(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Mine")
        create_todo(conn, 999, USER, "Theirs")
        assert [r["task"] for r in list_todos(conn, GUILD)] == ["Mine"]


def test_complete_records_who_and_when(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do it")
        assert complete_todo(conn, todo_id, GUILD, 4242, now_ts=555.0)
        row = _read(conn, todo_id)
    assert row["completed_at"] == 555.0
    assert row["completed_by"] == 4242


def test_complete_is_idempotent(db):
    """The board button and the dashboard can race; the second call is a no-op."""
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do it")
        assert complete_todo(conn, todo_id, GUILD, USER, now_ts=100.0)
        assert not complete_todo(conn, todo_id, GUILD, 999, now_ts=200.0)
        row = _read(conn, todo_id)
    assert row["completed_at"] == 100.0
    assert row["completed_by"] == USER


def test_complete_rejects_other_guilds(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Mine")
        assert not complete_todo(conn, todo_id, 999, USER)
        assert _read(conn, todo_id)["completed_at"] is None


def test_complete_missing_returns_false(db):
    with open_db(db) as conn:
        assert not complete_todo(conn, 4242, GUILD, USER)


def test_pending_count_is_scoped_and_uncapped_by_the_list_limit(db):
    """The board footer counts every outstanding task, not just the page it
    renders, so the count query is separate from the row query."""
    with open_db(db) as conn:
        for i in range(25):
            create_todo(conn, GUILD, USER, f"Task {i}")
        create_todo(conn, 999, USER, "Theirs")
        assert pending_count(conn, GUILD) == 25
        assert len(pending_todos(conn, GUILD, limit=16)) == 16
        assert pending_count(conn, 999) == 1


def test_completed_rows_leave_the_pending_list(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do it")
        complete_todo(conn, todo_id, GUILD, USER)
        assert pending_todos(conn, GUILD) == []


# ── board placement ───────────────────────────────────────────────────


def test_board_defaults_to_unposted(db):
    with open_db(db) as conn:
        board = get_board(conn, GUILD)
    assert board.channel_id == 0
    assert board.message_id == 0
    assert not board.posted


def test_save_and_read_board(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666, now_ts=42.0)
        board = get_board(conn, GUILD)
    assert (board.channel_id, board.message_id) == (555, 666)
    assert board.posted
    assert board.updated_at == 42.0


def test_save_board_upserts_in_place(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 1, 2)
        save_board(conn, GUILD, 3, 4)
        board = get_board(conn, GUILD)
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_board WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
    assert (board.channel_id, board.message_id) == (3, 4)
    assert count == 1


def test_clear_board_marks_unposted(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666)
        clear_board(conn, GUILD)
        board = get_board(conn, GUILD)
    assert not board.posted


def test_board_is_scoped_by_guild(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 1, 2)
        assert not get_board(conn, 999).posted


def test_guilds_with_board_lists_only_posted(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 1, 2)
        save_board(conn, 999, 0, 0)
        assert guilds_with_board(conn) == [GUILD]
