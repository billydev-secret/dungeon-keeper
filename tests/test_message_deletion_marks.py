"""Tests for deletion marking on the message archive.

The archive is permanent — a Discord deletion flags the row rather than
removing it — so these cover the flag's semantics rather than any erasure:
first-writer-wins attribution, idempotency under redelivered gateway events,
the source-scoped rollback for a delete that never happened, and the one path
that still hard-erases (a subject's Art 17 request).
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services.message_store import (
    DELETE_SOURCE_AUTO_DELETE,
    DELETE_SOURCE_DISCORD,
    clear_deleted_flag,
    init_message_tables,
    mark_messages_deleted,
    store_message,
)

GUILD = 1
OTHER_GUILD = 2
NOW = 1_700_000_000


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_message_tables(c)
    return c


def _store(conn: sqlite3.Connection, message_id: int, guild_id: int = GUILD) -> None:
    store_message(
        conn,
        message_id=message_id,
        guild_id=guild_id,
        channel_id=10,
        author_id=50,
        content="hello",
        reply_to_id=None,
        ts=1_000_000,
        attachment_urls=[],
        mention_ids=[],
    )


def _flag(conn: sqlite3.Connection, message_id: int) -> tuple:
    row = conn.execute(
        "SELECT deleted_at, deleted_source FROM messages WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return (row["deleted_at"], row["deleted_source"])


# ── Schema ────────────────────────────────────────────────────────────


def test_a_stored_message_starts_live(conn):
    _store(conn, 1)
    assert _flag(conn, 1) == (None, None)


def test_init_adds_the_columns_to_a_legacy_table(conn):
    """A DB predating the columns must gain them, not crash on the next write."""
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    legacy.execute(
        """
        CREATE TABLE messages (
            message_id  INTEGER PRIMARY KEY,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            author_id   INTEGER NOT NULL,
            content     TEXT,
            reply_to_id INTEGER,
            ts          INTEGER NOT NULL
        )
        """
    )
    init_message_tables(legacy)
    cols = {r[1] for r in legacy.execute("PRAGMA table_info(messages)").fetchall()}
    assert {"deleted_at", "deleted_source"} <= cols


# ── Marking ───────────────────────────────────────────────────────────


def test_marking_flags_the_row_without_removing_it(conn):
    _store(conn, 1)
    assert mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW) == 1
    assert _flag(conn, 1) == (NOW, DELETE_SOURCE_DISCORD)
    # The archive is permanent — the row and its content survive.
    row = conn.execute("SELECT content FROM messages WHERE message_id = 1").fetchone()
    assert row["content"] == "hello"


def test_marking_is_idempotent(conn):
    """A redelivered gateway event must be a no-op, not a re-stamp."""
    _store(conn, 1)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW)
    assert mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW + 500) == 0
    assert _flag(conn, 1) == (NOW, DELETE_SOURCE_DISCORD)  # original ts kept


def test_a_later_generic_event_cannot_overwrite_an_attributed_source(conn):
    """The whole reason auto-delete claims *before* calling the Discord API.

    The gateway event arrives moments later carrying no actor; without
    first-writer-wins every attributed deletion would be relabelled 'discord'.
    """
    _store(conn, 1)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE, NOW)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW + 1)
    assert _flag(conn, 1) == (NOW, DELETE_SOURCE_AUTO_DELETE)


def test_marking_is_guild_scoped(conn):
    _store(conn, 1, guild_id=OTHER_GUILD)
    assert mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW) == 0
    assert _flag(conn, 1) == (None, None)


def test_marking_an_unknown_message_is_a_no_op(conn):
    """Storage level 'none' guilds and pre-bot messages have no row to flag."""
    assert mark_messages_deleted(conn, GUILD, {999}, DELETE_SOURCE_DISCORD, NOW) == 0


def test_marking_nothing_is_a_no_op(conn):
    assert mark_messages_deleted(conn, GUILD, set(), DELETE_SOURCE_DISCORD, NOW) == 0


def test_bulk_marking_counts_only_newly_flagged_rows(conn):
    for mid in (1, 2, 3):
        _store(conn, mid)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW)
    assert mark_messages_deleted(conn, GUILD, {1, 2, 3}, DELETE_SOURCE_DISCORD, NOW) == 2


def test_an_unknown_source_is_rejected(conn):
    """A typo'd source would be invisible in the UI's badge mapping."""
    _store(conn, 1)
    with pytest.raises(ValueError, match="unknown delete source"):
        mark_messages_deleted(conn, GUILD, {1}, "definitely_not_a_source", NOW)


# ── Rollback ──────────────────────────────────────────────────────────


def test_clearing_undoes_an_optimistic_claim(conn):
    """Discord refused the delete — the message is still there, so the badge
    (and the suppressed deep link) would be a lie."""
    _store(conn, 1)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE, NOW)
    assert clear_deleted_flag(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE) == 1
    assert _flag(conn, 1) == (None, None)


def test_clearing_cannot_touch_another_source(conn):
    """Someone else deleted it mid-sweep; our rollback must not resurrect it."""
    _store(conn, 1)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW)
    assert clear_deleted_flag(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE) == 0
    assert _flag(conn, 1) == (NOW, DELETE_SOURCE_DISCORD)


def test_clearing_is_guild_scoped(conn):
    _store(conn, 1, guild_id=OTHER_GUILD)
    mark_messages_deleted(conn, OTHER_GUILD, {1}, DELETE_SOURCE_AUTO_DELETE, NOW)
    assert clear_deleted_flag(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE) == 0
    assert _flag(conn, 1) == (NOW, DELETE_SOURCE_AUTO_DELETE)


def test_clearing_nothing_is_a_no_op(conn):
    assert clear_deleted_flag(conn, GUILD, set(), DELETE_SOURCE_AUTO_DELETE) == 0


def test_claim_delete_release_round_trip_leaves_no_trace(conn):
    """The full optimistic sequence for a delete that never happened."""
    _store(conn, 1)
    mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE, NOW)
    clear_deleted_flag(conn, GUILD, {1}, DELETE_SOURCE_AUTO_DELETE)
    assert _flag(conn, 1) == (None, None)
    # And the row is still markable afterwards.
    assert mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW + 9) == 1


# ── Hard erasure still erases ─────────────────────────────────────────


def test_purge_user_data_still_removes_flagged_rows(tmp_path):
    """Art 17 is not satisfied by a flag — the subject's rows must actually go.

    Guards the failure mode this whole change could have introduced: swapping
    the archive to soft deletion and quietly taking the erasure path with it.
    """
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.privacy_service import purge_user_data
    from migrations import apply_migrations_sync

    db_path = tmp_path / "purge.db"
    apply_migrations_sync(db_path)

    with open_db(db_path) as conn:
        init_message_tables(conn)
        _store(conn, 1)
        _store(conn, 2)
        mark_messages_deleted(conn, GUILD, {1}, DELETE_SOURCE_DISCORD, NOW)

    with open_db(db_path) as conn:
        purge_user_data(conn, GUILD, 50)

    with open_db(db_path) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = 50",
            (GUILD,),
        ).fetchone()[0]
    assert remaining == 0, "a flagged message must still be hard-erased on request"
