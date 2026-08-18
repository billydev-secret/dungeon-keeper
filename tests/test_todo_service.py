"""Tests for services/todo_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.todo_service import (
    BOARD_ALL,
    BOARD_CHORES,
    clear_board,
    complete_todo,
    create_todo,
    get_board,
    conflicting_board,
    guilds_with_board,
    list_todos,
    mark_missed,
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


# ── The two boards are separate rows ────────────────────────────────────


def test_the_two_boards_are_independent(db):
    """Widening the key must not let one board's placement clobber the other's."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222, kind=BOARD_ALL)
        save_board(conn, GUILD, 333, 444, kind=BOARD_CHORES)

        assert get_board(conn, GUILD, BOARD_ALL).channel_id == 111
        assert get_board(conn, GUILD, BOARD_ALL).message_id == 222
        assert get_board(conn, GUILD, BOARD_CHORES).channel_id == 333
        assert get_board(conn, GUILD, BOARD_CHORES).message_id == 444


def test_get_board_defaults_to_the_all_todos_board(db):
    """The original board is the default kind, so pre-existing callers are unmoved."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        assert get_board(conn, GUILD).kind == BOARD_ALL
        assert get_board(conn, GUILD).channel_id == 111
        assert not get_board(conn, GUILD, BOARD_CHORES).posted


def test_clearing_one_board_leaves_the_other_posted(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222, kind=BOARD_ALL)
        save_board(conn, GUILD, 333, 444, kind=BOARD_CHORES)
        clear_board(conn, GUILD, kind=BOARD_CHORES)

        assert get_board(conn, GUILD, BOARD_ALL).posted
        assert not get_board(conn, GUILD, BOARD_CHORES).posted


def test_guilds_with_board_is_scoped_by_kind(db):
    """The refresh loop drives each panel from its own work list.

    A guild running only the chore board must not be handed to the all-todos
    panel's refresh, and vice versa.
    """
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222, kind=BOARD_ALL)
        save_board(conn, 999, 333, 444, kind=BOARD_CHORES)

        assert guilds_with_board(conn, BOARD_ALL) == [GUILD]
        assert guilds_with_board(conn, BOARD_CHORES) == [999]


# ── Two boards can never share a channel ────────────────────────────────


def test_conflicting_board_names_the_other_resident(db):
    """The guard that keeps the second sticky panel out of the first's channel.

    Neither board sets ``restick_on_bot``, so they cannot storm the way the two
    opted-in economy panels did (F1,
    docs/reviews/2026-08-06-sticky-panel-machinery.md). What they do instead is
    the *fix* for that storm turned against us: both wake on the same human
    message, race for the channel's one bottom slot, and the loser yields to
    ``core.sticky.was_placed`` — permanently, while the other keeps winning.
    So the collision is refused where a human can still read the reason.
    """
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)
        assert conflicting_board(conn, GUILD, BOARD_CHORES, 555) == (
            "the server todo board"
        )


def test_conflicting_board_catches_the_collision_from_either_side(db):
    """Whichever board is placed second is the one refused."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_CHORES)
        assert conflicting_board(conn, GUILD, BOARD_ALL, 555) == "the mod chore board"


def test_conflicting_board_allows_a_free_channel(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)
        assert conflicting_board(conn, GUILD, BOARD_CHORES, 777) is None


def test_a_board_does_not_conflict_with_itself(db):
    """Re-posting a board into the channel it already occupies is a move, not a clash."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)
        assert conflicting_board(conn, GUILD, BOARD_ALL, 555) is None


def test_an_unposted_board_frees_its_channel(db):
    """Removing one board must let the other take the channel it vacated."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)
        clear_board(conn, GUILD, kind=BOARD_ALL)
        assert conflicting_board(conn, GUILD, BOARD_CHORES, 555) is None


def test_unposting_can_never_conflict(db):
    """channel_id 0 means "take it down" — there is nothing to collide with."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)
        assert conflicting_board(conn, GUILD, BOARD_CHORES, 0) is None


def test_conflicting_board_is_scoped_by_guild(db):
    """Another server's board in the same channel id is not this server's problem."""
    with open_db(db) as conn:
        save_board(conn, 999, 555, 222, kind=BOARD_ALL)
        assert conflicting_board(conn, GUILD, BOARD_CHORES, 555) is None


# ── The third state ─────────────────────────────────────────────────────


def test_mark_missed_closes_a_row_without_crediting_it(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        assert mark_missed(conn, todo_id, now_ts=1000.0) is True
        row = _read(conn, todo_id)
    assert row["missed_at"] == 1000.0
    assert row["completed_at"] is None
    assert not row["completed_by"]


def test_mark_missed_is_idempotent(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        assert mark_missed(conn, todo_id, now_ts=1000.0) is True
        assert mark_missed(conn, todo_id, now_ts=2000.0) is False
        assert _read(conn, todo_id)["missed_at"] == 1000.0


def test_mark_missed_refuses_a_completed_row(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        complete_todo(conn, todo_id, GUILD, USER, now_ts=500.0)
        assert mark_missed(conn, todo_id, now_ts=1000.0) is False
        assert _read(conn, todo_id)["missed_at"] is None


def test_complete_refuses_a_missed_row(db):
    """Yesterday's box is not tickable today."""
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        mark_missed(conn, todo_id, now_ts=1000.0)
        assert complete_todo(conn, todo_id, GUILD, USER, now_ts=2000.0) is False
        assert _read(conn, todo_id)["completed_at"] is None


def test_missed_rows_leave_the_pending_list_and_count(db):
    with open_db(db) as conn:
        keep = create_todo(conn, GUILD, USER, "Still open")
        drop = create_todo(conn, GUILD, USER, "Written off")
        mark_missed(conn, drop, now_ts=1000.0)

        assert [r["id"] for r in pending_todos(conn, GUILD)] == [keep]
        assert pending_count(conn, GUILD) == 1


def test_missed_rows_are_not_pending_in_the_dashboard_filter(db):
    """The dashboard's "pending" tab has to agree with the board."""
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Still open")
        drop = create_todo(conn, GUILD, USER, "Written off")
        mark_missed(conn, drop, now_ts=1000.0)

        pending = list_todos(conn, GUILD, status="pending")
        assert [r["task"] for r in pending] == ["Still open"]


def test_missed_rows_are_not_completed_either(db):
    """A written-off chore is closed, but nobody did it — it is not a completion."""
    with open_db(db) as conn:
        drop = create_todo(conn, GUILD, USER, "Written off")
        mark_missed(conn, drop, now_ts=1000.0)
        assert list_todos(conn, GUILD, status="completed") == []
        assert len(list_todos(conn, GUILD)) == 1
