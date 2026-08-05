"""Tests for services/privacy_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from tests.db_template import migrated_db
from bot_modules.services.privacy_service import purge_user_data
from bot_modules.services.usage_telemetry_service import (
    KIND_COMMAND,
    KIND_PANEL,
    record_event,
)

GUILD = 123
USER = 1001
OTHER_USER = 1002


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
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


def test_deletes_usage_events(db):
    """Usage telemetry is retained indefinitely, so this hard-erasure path is
    the only thing that ever clears it — if it drops out of the purge list the
    data becomes genuinely unreachable."""
    with open_db(db) as conn:
        record_event(conn, GUILD, KIND_COMMAND, "bank", USER)
        record_event(conn, GUILD, KIND_PANEL, "home", USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert row == 0


def test_does_not_delete_other_users_usage_events(db):
    with open_db(db) as conn:
        record_event(conn, GUILD, KIND_COMMAND, "bank", USER)
        record_event(conn, GUILD, KIND_COMMAND, "bank", OTHER_USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE guild_id = ? AND user_id = ?",
            (GUILD, OTHER_USER),
        ).fetchone()[0]
    assert row == 1


def test_idempotent_on_empty_db(db):
    with open_db(db) as conn:
        count = purge_user_data(conn, GUILD, USER)
    assert count == 0


# ── keep_messages=True ────────────────────────────────────────────────


def test_keep_messages_preserves_message_rows(db):
    """/delete_me uses keep_messages=True to keep the user's local archive."""
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, USER, 2)
        count = purge_user_data(conn, GUILD, USER, keep_messages=True)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    # Returns the count for the summary even though nothing was deleted.
    assert count == 2
    assert remaining == 2


def test_keep_messages_still_clears_xp(db):
    """Other PII (XP, activity, profile) is purged even when messages are kept."""
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_xp(conn, GUILD, USER)
        _insert_known_user(conn, GUILD, USER)
        purge_user_data(conn, GUILD, USER, keep_messages=True)
        xp_remaining = conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
        known_remaining = conn.execute(
            "SELECT COUNT(*) FROM known_users WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert xp_remaining == 0
    assert known_remaining == 0


# ── heavy users must not break the erasure (SQLite variable cap) ───────


def test_purges_user_with_more_messages_than_sqlite_variable_cap(db):
    """A heavy poster has more message rows than SQLite's 32,766-variable cap.

    The old implementation inlined every message id into one ``IN (?,…)``,
    so a genuine legal-erasure run raised ``too many SQL variables`` for
    exactly the accounts most likely to file one. 2026-08 review, privacy A1.
    """
    n = 33_000
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts) "
            "VALUES (?, ?, 500, ?, 1000.0)",
            ((i + 1, GUILD, USER) for i in range(n)),
        )
        # Child rows on both ends of the id range so chunking is exercised.
        conn.executemany(
            "INSERT INTO message_attachments (message_id, url) VALUES (?, 'u')",
            [(1,), (n,)],
        )
        count = purge_user_data(conn, GUILD, USER)
        msgs = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE author_id = ?", (USER,)
        ).fetchone()[0]
        children = conn.execute(
            "SELECT COUNT(*) FROM message_attachments"
        ).fetchone()[0]
    assert count == n
    assert msgs == 0
    assert children == 0


# ── tables added by the 2026-08 review (previously missed) ─────────────


@pytest.mark.parametrize(
    ("table", "insert_sql", "params", "where"),
    [
        pytest.param(
            "xp_reaction_awards",
            "INSERT INTO xp_reaction_awards (guild_id, message_id, user_id) VALUES (?, ?, ?)",
            (GUILD, 1, USER),
            "guild_id = ? AND user_id = ?",
            id="xp_reaction_awards",
        ),
        pytest.param(
            "member_birthdays",
            "INSERT INTO member_birthdays (guild_id, user_id, birth_month, birth_day, set_by, set_at) "
            "VALUES (?, ?, 6, 15, ?, 0)",
            (GUILD, USER, USER),
            "guild_id = ? AND user_id = ?",
            id="member_birthdays",
        ),
        pytest.param(
            "voice_master_profiles",
            "INSERT INTO voice_master_profiles (guild_id, user_id, saved_name, updated_at) VALUES (?, ?, 'room', 0)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="voice_master_profiles",
        ),
        pytest.param(
            "bios",
            "INSERT INTO bios (user_id, guild_id, message_id, channel_id) VALUES (?, ?, 1, 2)",
            (USER, GUILD),
            "guild_id = ? AND user_id = ?",
            id="bios",
        ),
        pytest.param(
            "bio_answers",
            "INSERT INTO bio_answers (user_id, guild_id, slot, question_id, question_text, answer) "
            "VALUES (?, ?, 0, 1, 'q', 'a')",
            (USER, GUILD),
            "guild_id = ? AND user_id = ?",
            id="bio_answers",
        ),
        pytest.param(
            "bio_field_values",
            "INSERT INTO bio_field_values (user_id, guild_id, field_id, field_label, value) "
            "VALUES (?, ?, 1, 'l', 'v')",
            (USER, GUILD),
            "guild_id = ? AND user_id = ?",
            id="bio_field_values",
        ),
    ],
)
def test_purges_review_added_simple_tables(db, table, insert_sql, params, where):
    with open_db(db) as conn:
        conn.execute(insert_sql, params)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", (GUILD, USER)
        ).fetchone()[0]
    assert remaining == 0


@pytest.mark.parametrize(
    ("table", "col_a", "col_b"),
    [
        pytest.param("watched_users", "watched_user_id", "watcher_user_id", id="watched_users"),
        pytest.param("voice_master_trusted", "owner_id", "target_id", id="voice_master_trusted"),
        pytest.param("invite_edges", "inviter_id", "invitee_id", id="invite_edges"),
    ],
)
def test_purges_two_sided_tables_in_both_directions(db, table, col_a, col_b):
    """Pair tables are cleared whichever side the erased user is on."""
    extra = {
        "invite_edges": ", joined_at, invite_code) VALUES (?, ?, ?, 0, 'c')",
        "voice_master_trusted": ", added_at) VALUES (?, ?, ?, 0)",
        "watched_users": ") VALUES (?, ?, ?)",
    }[table]
    with open_db(db) as conn:
        conn.execute(
            f"INSERT INTO {table} (guild_id, {col_a}, {col_b}{extra}",
            (GUILD, USER, OTHER_USER),
        )
        conn.execute(
            f"INSERT INTO {table} (guild_id, {col_a}, {col_b}{extra}",
            (GUILD, OTHER_USER + 1, USER),
        )
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col_a} = ? OR {col_b} = ?",
            (USER, USER),
        ).fetchone()[0]
    assert remaining == 0


def test_purge_covers_economy_per_member_state(db):
    """purge_user_data delegates econ/casino per-member rows to econ_purge_user
    — the ledger itself is deliberately preserved (audit integrity)."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, updated_at) "
            "VALUES (?, ?, 100, 0, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at) "
            "VALUES (?, ?, 100, 'test', 0)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        wallets = conn.execute(
            "SELECT COUNT(*) FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT COUNT(*) FROM econ_ledger WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert wallets == 0
    assert ledger == 1  # preserved on purpose
