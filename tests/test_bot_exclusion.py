"""Unit tests for bot_modules.core.bot_exclusion.

The helper is three lines of string building, so the interesting coverage is
not the fragment's text but its *behaviour when spliced into real SQL* against a
migrated DB: that it drops bot rows, keeps humans, keeps authors with no
``known_users`` row at all, and is a genuine no-op under ``include_bots``.
"""

from __future__ import annotations

import pytest

from bot_modules.core.bot_exclusion import bot_filter_clause, bot_ids_subquery
from bot_modules.core.db_utils import open_db
from tests.db_template import migrated_db


GUILD = 10
HUMAN = 1
BOT = 2
ORPHAN = 3  # posts, but has no known_users row


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "bx.db"
    migrated_db(path)
    with open_db(path) as conn:
        for uid, is_bot in ((HUMAN, 0), (BOT, 1)):
            conn.execute(
                "INSERT OR REPLACE INTO known_users "
                "(guild_id, user_id, username, display_name, updated_at, is_bot,"
                " current_member) VALUES (?,?,?,?,?,?,1)",
                (GUILD, uid, f"u{uid}", f"u{uid}", 0.0, is_bot),
            )
        for mid, aid in ((100, HUMAN), (101, BOT), (102, BOT), (103, ORPHAN)):
            conn.execute(
                "INSERT INTO messages "
                "(message_id, guild_id, channel_id, author_id, content, ts)"
                " VALUES (?,?,?,?,?,?)",
                (mid, GUILD, 500, aid, "x", 1000),
            )
        yield conn


def _count(conn, clause: str, params: tuple) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE guild_id=?{clause}",
        (GUILD, *params),
    ).fetchone()[0]


def test_excludes_bot_rows_by_default(db_conn):
    clause, params = bot_filter_clause(GUILD)
    # 4 messages seeded, 2 of them from BOT.
    assert _count(db_conn, clause, params) == 2


def test_include_bots_is_a_byte_identical_noop(db_conn):
    clause, params = bot_filter_clause(GUILD, include_bots=True)
    assert (clause, params) == ("", ())
    assert _count(db_conn, clause, params) == 4


def test_orphan_authors_count_as_human(db_conn):
    """An author with no known_users row survives the filter.

    This is deliberate, not an oversight: in prod the 40 such authors are
    departed *humans* (39 of 40 have XP rows, and no bot has ever earned XP).
    Pinning the behaviour so a future change to NOT IN semantics is caught.
    """
    clause, params = bot_filter_clause(GUILD)
    rows = db_conn.execute(
        f"SELECT author_id FROM messages WHERE guild_id=?{clause}",
        (GUILD, *params),
    ).fetchall()
    assert ORPHAN in {r[0] for r in rows}


def test_other_guilds_bots_are_not_excluded(db_conn):
    """The subquery is guild-scoped — a bot in guild A must not filter guild B."""
    db_conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, content, ts)"
        " VALUES (?,?,?,?,?,?)",
        (200, GUILD + 1, 500, BOT, "x", 1000),
    )
    clause, params = bot_filter_clause(GUILD + 1)
    got = db_conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE guild_id=?{clause}",
        (GUILD + 1, *params),
    ).fetchone()[0]
    assert got == 1


@pytest.mark.parametrize(
    "column,expected_prefix",
    [
        ("author_id", " AND author_id NOT IN ("),
        ("m.author_id", " AND m.author_id NOT IN ("),
        ("from_user_id", " AND from_user_id NOT IN ("),
    ],
)
def test_column_is_honoured(column, expected_prefix):
    clause, params = bot_filter_clause(GUILD, column=column)
    assert clause.startswith(expected_prefix)
    assert params == (GUILD,)


def test_aliased_column_works_against_real_sql(db_conn):
    clause, params = bot_filter_clause(GUILD, column="m.author_id")
    got = db_conn.execute(
        f"SELECT COUNT(*) FROM messages m WHERE m.guild_id=?{clause}",
        (GUILD, *params),
    ).fetchone()[0]
    assert got == 2


def test_subquery_helper_matches_clause_body(db_conn):
    rows = db_conn.execute(bot_ids_subquery(), (GUILD,)).fetchall()
    assert {r[0] for r in rows} == {BOT}
