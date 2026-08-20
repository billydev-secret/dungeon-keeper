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
        pytest.param(
            "pen_pals_pool_events",
            "INSERT INTO pen_pals_pool_events (guild_id, user_id, at, action, reason) "
            "VALUES (?, ?, 0, 'join', 'panel')",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="pen_pals_pool_events",
        ),
        pytest.param(
            "pen_pals_optouts",
            "INSERT INTO pen_pals_optouts (guild_id, user_id, at) VALUES (?, ?, 0)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="pen_pals_optouts",
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


def test_purge_covers_music_playlist_member_rows(db):
    """purge_user_data delegates the music playlist's `added_by` rows to
    music_playlist_store.purge_member_rows — the generic user_id sweep can't
    reach them (register: docs/data_register.md). Reviewer references are
    nulled, other members' rows survive."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO music_playlist_tracks (guild_id, playlist_id, track_id, "
            "channel_id, message_id, added_by, added_at) VALUES (?, 'pl', 't1', 5, 6, ?, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO music_playlist_tracks (guild_id, playlist_id, track_id, "
            "channel_id, message_id, added_by, added_at) VALUES (?, 'pl', 't2', 5, 7, ?, 0)",
            (GUILD, OTHER_USER),
        )
        conn.execute(
            "INSERT INTO music_playlist_unmatched (guild_id, channel_id, message_id, "
            "source_url, added_by, created_at) VALUES (?, 5, 8, 'https://x/1', ?, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO music_playlist_unmatched (guild_id, channel_id, message_id, "
            "source_url, added_by, status, reviewed_by, reviewed_at, created_at) "
            "VALUES (?, 5, 9, 'https://x/2', ?, 'approved', ?, 0, 0)",
            (GUILD, OTHER_USER, USER),
        )
        purge_user_data(conn, GUILD, USER)
        mine = conn.execute(
            "SELECT (SELECT COUNT(*) FROM music_playlist_tracks WHERE added_by = ?) + "
            "(SELECT COUNT(*) FROM music_playlist_unmatched WHERE added_by = ?)",
            (USER, USER),
        ).fetchone()[0]
        others = conn.execute(
            "SELECT (SELECT COUNT(*) FROM music_playlist_tracks WHERE added_by = ?) + "
            "(SELECT COUNT(*) FROM music_playlist_unmatched WHERE added_by = ?)",
            (OTHER_USER, OTHER_USER),
        ).fetchone()[0]
        reviewer = conn.execute(
            "SELECT reviewed_by FROM music_playlist_unmatched WHERE added_by = ?",
            (OTHER_USER,),
        ).fetchone()[0]
    assert mine == 0
    assert others == 2  # other members' rows survive
    assert reviewer is None  # the erased member's reviewer reference is nulled


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


def test_purge_anonymises_todos_rather_than_deleting_them(db):
    """A todos row is the team's work plus two ids naming a person.

    Deleting it to erase the ids would take real outstanding work off other
    people's list — a task someone else is part-way through vanishing because
    an unrelated member left. Clearing the ids erases everything identifying
    while the work stands (register: docs/data_register.md).
    """
    from bot_modules.services.todo_service import complete_todo, create_todo

    with open_db(db) as conn:
        mine = create_todo(conn, GUILD, USER, "Post the QOTD")
        theirs = create_todo(conn, GUILD, OTHER_USER, "Rotate the tunnel token")
        ticked = create_todo(conn, GUILD, OTHER_USER, "Check the mod queue")
        complete_todo(conn, ticked, GUILD, USER)

        purge_user_data(conn, GUILD, USER)

        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, task, added_by, completed_by FROM todos"
            ).fetchall()
        }

    # Nothing was removed — the work survives the erasure.
    assert set(rows) == {mine, theirs, ticked}
    assert rows[mine]["task"] == "Post the QOTD"
    # …but nothing still names the erased member.
    assert rows[mine]["added_by"] == 0
    assert rows[ticked]["completed_by"] is None
    # Another member's rows are untouched.
    assert rows[theirs]["added_by"] == OTHER_USER
    assert rows[ticked]["added_by"] == OTHER_USER


def test_purge_of_todos_is_scoped_to_the_guild(db):
    from bot_modules.services.todo_service import create_todo

    with open_db(db) as conn:
        here = create_todo(conn, GUILD, USER, "Post the QOTD")
        elsewhere = create_todo(conn, 999, USER, "Their chore")
        purge_user_data(conn, GUILD, USER)
        rows = {
            r["id"]: r["added_by"]
            for r in conn.execute("SELECT id, added_by FROM todos").fetchall()
        }
    assert rows[here] == 0
    assert rows[elsewhere] == USER


def test_export_sees_a_task_the_member_completed(db):
    """`completed_by` had to join SUBJECT_ID_COLUMNS or an access request would
    show the tasks a member *added* and silently omit the ones they did."""
    from bot_modules.services.todo_service import complete_todo, create_todo

    assert "completed_by" in SUBJECT_ID_COLUMNS
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, OTHER_USER, "Check the mod queue")
        complete_todo(conn, todo_id, GUILD, USER)
        result = export_user_data(conn, GUILD, USER)

    tasks = result["tables"]["todos"]["rows"]
    assert [r["id"] for r in tasks] == [todo_id]


def test_purge_blanks_the_recurring_definition_so_it_stays_erased(db):
    """Regression: the anonymisation used to undo itself.

    `_spawn_one` stamps a definition's `created_by` onto every todos row it
    materialises. Scrubbing only `todos` left the id in `todo_recurring`, so
    the next scheduled fire wrote it straight back into `todos.added_by` — and
    every day after. The register's "nothing identifying survives" claim
    depends on this.
    """
    from bot_modules.services.todo_recurring_service import create_recurring, spawn_due

    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=0, created_by=USER, now_ts=0.0,
        )
        purge_user_data(conn, GUILD, USER)

        assert conn.execute(
            "SELECT created_by FROM todo_recurring"
        ).fetchone()["created_by"] == 0

        # The next fire must not resurrect the id.
        spawn_due(conn, now_ts=86_400.0, offset_hours_for=lambda _g: 0.0)
        added = [r["added_by"] for r in conn.execute("SELECT added_by FROM todos")]

    assert added and all(a == 0 for a in added)


def test_purge_of_definitions_is_scoped_to_the_guild(db):
    from bot_modules.services.todo_recurring_service import create_recurring

    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Mine", recurrence="daily",
            time_of_day=0, created_by=USER, now_ts=0.0,
        )
        create_recurring(
            conn, 999, task="Theirs", recurrence="daily",
            time_of_day=0, created_by=USER, now_ts=0.0,
        )
        purge_user_data(conn, GUILD, USER)
        rows = {
            r["task"]: r["created_by"]
            for r in conn.execute("SELECT task, created_by FROM todo_recurring")
        }
    assert rows["Mine"] == 0
    assert rows["Theirs"] == USER


# ── Risky Rolls (registered 2026-08-20) ──────────────────────────────


def _insert_risky_round(conn, guild_id, *, game_id, opener_id, rolls, **seats):
    conn.execute(
        "INSERT INTO risky_active_rounds "
        "(game_id, channel_id, guild_id, opener_id, is_open, highest_user, "
        " lowest_user, second_lowest_user, second_highest_user) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            game_id, 500, guild_id, opener_id,
            seats.get("highest_user"), seats.get("lowest_user"),
            seats.get("second_lowest_user"), seats.get("second_highest_user"),
        ),
    )
    for uid, roll in rolls.items():
        conn.execute(
            "INSERT INTO risky_round_rolls (game_id, user_id, roll) VALUES (?, ?, ?)",
            (game_id, uid, roll),
        )


def test_purge_clears_risky_round_the_member_rolled_in(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=OTHER_USER,
            rolls={USER: 40, OTHER_USER: 90},
            highest_user=OTHER_USER, lowest_user=USER,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 0
        # The rolls go with the round: this connection makes no foreign_keys
        # promise, so the cascade must not be what removes them.
        assert conn.execute("SELECT COUNT(*) FROM risky_round_rolls").fetchone()[0] == 0


def test_purge_clears_risky_round_the_member_only_opened(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=USER,
            rolls={OTHER_USER: 90, 1003: 10},
            highest_user=OTHER_USER, lowest_user=1003,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 0


@pytest.mark.parametrize("seat", ["second_lowest_user", "second_highest_user"])
def test_purge_clears_risky_round_naming_the_member_in_a_second_seat(db, seat):
    # The 100 and 1 rules seat a member who is neither winner nor loser.
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=OTHER_USER,
            rolls={OTHER_USER: 100, 1003: 1},
            highest_user=OTHER_USER, lowest_user=1003, **{seat: USER},
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 0


def test_purge_leaves_a_risky_round_the_member_had_no_part_in(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=OTHER_USER,
            rolls={OTHER_USER: 90, 1003: 10},
            highest_user=OTHER_USER, lowest_user=1003,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM risky_round_rolls").fetchone()[0] == 2


def test_purge_leaves_another_guilds_risky_round(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, 999, game_id="g1", opener_id=USER, rolls={USER: 40},
            highest_user=USER,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 1


def _insert_pending_question(conn, guild_id, *, winner_id, **csv_columns):
    """A pending question whose CSV columns default to naming nobody.

    ``participant_user_ids`` is NOT NULL, so it carries a bystander id unless
    the caller is the one putting the subject there.
    """
    columns = {"participant_user_ids": "1003"} | csv_columns
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO risky_pending_questions "
        f"(game_id, channel_id, guild_id, winner_id, prompt_kind, {names}) "
        f"VALUES ('g1', 500, ?, ?, 'direct', {marks})",
        (guild_id, winner_id, *columns.values()),
    )


@pytest.mark.parametrize(
    "column", ["participant_user_ids", "lowest_tie_user_ids", "questioners_asked"]
)
def test_purge_clears_pending_question_naming_the_member_in_a_csv_column(db, column):
    with open_db(db) as conn:
        _insert_pending_question(
            conn, GUILD, winner_id=OTHER_USER, **{column: str(USER)}
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_pending_questions").fetchone()[0] == 0


def test_purge_does_not_match_a_csv_id_by_substring(db):
    # USER is 1001; a round naming 10010 and 10011 must survive a bare-LIKE bug.
    with open_db(db) as conn:
        _insert_pending_question(
            conn, GUILD, winner_id=OTHER_USER,
            participant_user_ids=f"{USER}0,{USER}1",
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_pending_questions").fetchone()[0] == 1


def test_purge_clears_posted_question_the_member_asked(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO risky_posted_questions "
            "(message_id, channel_id, guild_id, asker_id, allowed_replier_ids, question_text) "
            "VALUES (7, 500, ?, ?, ?, 'their own words')",
            (GUILD, USER, str(OTHER_USER)),
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_posted_questions").fetchone()[0] == 0


def test_purge_clears_posted_question_the_member_was_only_asked(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO risky_posted_questions "
            "(message_id, channel_id, guild_id, asker_id, allowed_replier_ids, question_text) "
            "VALUES (7, 500, ?, ?, ?, 'someone elses words')",
            (GUILD, OTHER_USER, f"{USER},1003"),
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_posted_questions").fetchone()[0] == 0

