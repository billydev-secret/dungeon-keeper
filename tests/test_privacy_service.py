"""Tests for services/privacy_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from tests.db_template import migrated_db
from bot_modules.services.privacy_service import (
    SUBJECT_ID_COLUMNS,
    _MESSAGE_CHILD_TABLES,
    export_user_data,
    purge_user_data,
)
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


# ── subject access export (GDPR Art 15 / Art 20) ───────────────────────


def test_export_finds_rows_by_conventional_subject_column(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        _insert_known_user(conn, GUILD, USER)
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["member_xp"] == 1
    assert data["counts"]["known_users"] == 1
    assert data["tables"]["member_xp"]["rows"][0]["total_xp"] == 100


def test_export_excludes_other_users_and_other_guilds(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        _insert_xp(conn, GUILD, OTHER_USER)
        _insert_xp(conn, GUILD + 1, USER)
        data = export_user_data(conn, GUILD, USER)
    rows = data["tables"]["member_xp"]["rows"]
    assert len(rows) == 1
    assert rows[0]["user_id"] == USER
    assert rows[0]["guild_id"] == GUILD


def test_export_is_empty_for_unknown_user(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, OTHER_USER)
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"] == {}
    assert data["tables"] == {}


def test_export_includes_ledger_the_purge_deliberately_preserves(db):
    """Art 17(3) retention is not an Art 15 exemption: rows the server keeps
    are still the subject's data and must still be disclosable."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at) "
            "VALUES (?, ?, 100, 'test', 0)",
            (GUILD, USER),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["econ_ledger"] == 1


def test_export_reaches_message_children_through_the_author(db):
    """message_sentiment has no subject column — it is only the subject's data
    by virtue of hanging off their message, so column discovery cannot find it
    and the message-id join must."""
    with open_db(db) as conn:
        mid = _insert_message(conn, GUILD, USER)
        conn.execute(
            "INSERT INTO message_sentiment "
            "(message_id, guild_id, channel_id, sentiment, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, GUILD, 500, 0.5, 1000.0),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["messages"] == 1
    assert data["counts"]["message_sentiment"] == 1


def test_export_message_children_chunk_past_the_variable_cap(db):
    """Same failure mode the purge had (A1): >32,766 ids inlined into one
    IN (...) raises. The heaviest posters are the likeliest requesters."""
    with open_db(db) as conn:
        for i in range(33_000):
            _insert_message(conn, GUILD, USER, message_id=9_000_000 + i)
        conn.commit()
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["messages"] == 33_000


def test_export_flags_tables_naming_a_second_member(db):
    """Art 15(4): the operator has to decide about the counterparty before
    disclosure, so the export must surface those tables rather than bury them."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO invite_edges (guild_id, inviter_id, invitee_id, joined_at) "
            "VALUES (?, ?, ?, 0)",
            (GUILD, USER, OTHER_USER),
        )
        data = export_user_data(conn, GUILD, USER)
    assert "invite_edges" in data["review_required"]


def test_export_finds_the_subject_on_either_side_of_a_pair_table(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO invite_edges (guild_id, inviter_id, invitee_id, joined_at) "
            "VALUES (?, ?, ?, 0)",
            (GUILD, OTHER_USER, USER),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["invite_edges"] == 1


def test_export_covers_every_table_the_purge_deletes(db):
    """The load-bearing property: an access export that misses a table the
    erasure path knows about is an incomplete answer to a statutory request.
    Seeds a row in each purge-covered table, then asserts the export sees it.
    New tables joining the purge fail here until the export can reach them."""
    with open_db(db) as conn:
        purge_tables = _tables_touched_by_purge(conn)
        missing = []
        for table in sorted(purge_tables):
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if not (cols & SUBJECT_ID_COLUMNS) and table not in _MESSAGE_CHILD_TABLES:
                missing.append(table)
    assert not missing, (
        "purge-covered tables the export's column discovery cannot reach: "
        f"{missing} — add the column to SUBJECT_ID_COLUMNS or reach the table "
        "explicitly, as the message children are"
    )


def _tables_touched_by_purge(conn):
    """Every table name purge_user_data (and econ_purge_user) writes to, read
    off the source rather than restated — a second copy is how the two drift."""
    import re
    from pathlib import Path

    import bot_modules.services.privacy_service as ps
    import bot_modules.services.economy_service as es

    names = set()
    for mod in (ps, es):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        names |= set(re.findall(r"DELETE FROM (\w+)", src))
        names |= set(re.findall(r'DELETE FROM \{?"?(\w+)"?\}?', src))
    real = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    names |= set(ps._MESSAGE_CHILD_TABLES)
    names |= set(getattr(es, "_PURGE_USER_ID_TABLES", ()))
    return {n for n in names if n in real}


def test_purge_strips_from_user_chips_from_mention_award_rules(db):
    """A from_user chip names a member inside the conditions JSON — the
    "list-column blind spot": invisible to SUBJECT_ID_COLUMNS. Erasure must
    strip the chip; a rule left with no chips is deleted outright (an empty
    chip list is the fail-closed "matches nothing" state, and keeping a husk
    that exists only because of the erased member serves nobody).
    """
    from bot_modules.mention_awards.logic import Condition
    from bot_modules.mention_awards.store import create_rule, list_rules

    with open_db(db) as conn:
        # Rule A: from_user chip on the erased member + a text chip — chip
        # stripped, rule survives.
        a = create_rule(
            conn, GUILD, channel_id=42, amount=10,
            conditions=[
                Condition("from_user", str(USER)),
                Condition("contains_text", "your turn"),
            ],
        )
        # Rule B: ONLY a from_user chip on the erased member — rule deleted.
        create_rule(
            conn, GUILD, channel_id=43, amount=10,
            conditions=[Condition("from_user", str(USER))],
        )
        # Rule C: someone else's from_user chip — untouched.
        c = create_rule(
            conn, GUILD, channel_id=44, amount=10,
            conditions=[Condition("from_user", str(OTHER_USER))],
        )

        purge_user_data(conn, GUILD, USER)

        rows = {int(r["id"]): r for r in list_rules(conn, GUILD)}
        assert set(rows) == {a, c}
        assert str(USER) not in rows[a]["conditions"]
        assert "your turn" in rows[a]["conditions"]
        assert str(OTHER_USER) in rows[c]["conditions"]
