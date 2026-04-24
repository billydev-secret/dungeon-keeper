"""Tests for services/privacy_service.py."""

from __future__ import annotations

import pytest

from db_utils import open_db
from migrations import apply_migrations_sync
from services.privacy_service import purge_user_data

GUILD = 123
USER = 1001
OTHER_USER = 1002


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _insert_message(conn, guild_id, user_id, message_id=None):
    mid = message_id or (user_id * 1000 + guild_id)
    conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(message_id, guild_id, channel_id, author_id, ts) VALUES (?, ?, ?, ?, ?)",
        (mid, guild_id, 500, user_id, 1000.0),
    )
    return mid


def _insert_xp(conn, guild_id, user_id):
    conn.execute(
        "INSERT OR IGNORE INTO member_xp (guild_id, user_id, total_xp) VALUES (?, ?, ?)",
        (guild_id, user_id, 100),
    )


def _insert_known_user(conn, guild_id, user_id):
    conn.execute(
        "INSERT OR IGNORE INTO known_users (guild_id, user_id, username, display_name, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, f"user_{user_id}", f"User {user_id}", 1000.0),
    )


# ── message count return value ─────────────────────────────────────────


def test_returns_message_count(db):
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, USER, 2)
        count = purge_user_data(conn, GUILD, USER)
    assert count == 2


def test_returns_zero_for_user_with_no_messages(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        count = purge_user_data(conn, GUILD, USER)
    assert count == 0


# ── messages deleted ──────────────────────────────────────────────────


def test_deletes_messages(db):
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert remaining == 0


def test_does_not_delete_other_users_messages(db):
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, OTHER_USER, 2)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (GUILD, OTHER_USER),
        ).fetchone()[0]
    assert remaining == 1


def test_does_not_delete_other_guilds_messages(db):
    other_guild = 999
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, other_guild, USER, 2)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (other_guild, USER),
        ).fetchone()[0]
    assert remaining == 1


# ── core tables deleted ───────────────────────────────────────────────


def test_deletes_member_xp(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert row == 0


def test_deletes_known_users(db):
    with open_db(db) as conn:
        _insert_known_user(conn, GUILD, USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM known_users WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert row == 0


def test_idempotent_on_empty_db(db):
    with open_db(db) as conn:
        count = purge_user_data(conn, GUILD, USER)
    assert count == 0
