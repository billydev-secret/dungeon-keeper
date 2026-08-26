"""Migration 180: the two todo boards become one.

The interesting part is the merge rule. Two rows per guild collapse to one, and
which one survives decides where the board a server actually uses ends up — a
wrong answer moves a live board into the wrong channel, or drops the placement
entirely and leaves the board silently unposted.

Pinned here against production's real shape (guild 1469…666, read read-only
before the migration was written): the all-todos board was never placed and the
chore board is live, so the chore board's channel must survive.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 1469491362444480666
OTHER_GUILD = 1476525656115515484

# Production, verbatim: `all` never posted, `chores` live.
PROD_CHORE_CHANNEL = 1531045313807126638
PROD_CHORE_MESSAGE = 1539352171055947847


def _apply_before_180(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "180"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _seed(db_path, rows) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO todo_board (guild_id, kind, channel_id, message_id,"
            " updated_at) VALUES (?, ?, ?, ?, 0)",
            rows,
        )
        conn.commit()


def _boards(db_path) -> dict[int, tuple[int, int]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            int(r["guild_id"]): (int(r["channel_id"]), int(r["message_id"]))
            for r in conn.execute("SELECT * FROM todo_board")
        }


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "merge.db"
    _apply_before_180(path, monkeypatch)
    return path


def test_production_keeps_the_chore_board_that_is_actually_posted(db, monkeypatch):
    """The all-todos board was never placed; losing the live one would unpost
    the only board this server has."""
    _seed(db, [
        (GUILD, "all", 0, 0),
        (GUILD, "chores", PROD_CHORE_CHANNEL, PROD_CHORE_MESSAGE),
    ])
    migrations.apply_migrations_sync(db)
    assert _boards(db) == {GUILD: (PROD_CHORE_CHANNEL, PROD_CHORE_MESSAGE)}


def test_snowflake_ids_survive_the_rebuild(db):
    """Channel and message ids are past 2^53; a float round-trip would corrupt
    them into a message that does not exist."""
    _seed(db, [(GUILD, "chores", PROD_CHORE_CHANNEL, PROD_CHORE_MESSAGE)])
    migrations.apply_migrations_sync(db)
    channel, message = _boards(db)[GUILD]
    assert channel == PROD_CHORE_CHANNEL
    assert message == PROD_CHORE_MESSAGE


def test_a_lone_all_board_is_kept(db):
    _seed(db, [(GUILD, "all", 111, 222), (GUILD, "chores", 0, 0)])
    migrations.apply_migrations_sync(db)
    assert _boards(db) == {GUILD: (111, 222)}


def test_when_both_are_posted_the_chore_channel_wins(db):
    """Chores are mod-facing and the merged board carries them, so landing it
    in a public channel would disclose more than the other way round."""
    _seed(db, [(GUILD, "all", 111, 222), (GUILD, "chores", 333, 444)])
    migrations.apply_migrations_sync(db)
    assert _boards(db) == {GUILD: (333, 444)}


def test_neither_posted_collapses_to_one_unposted_row(db):
    _seed(db, [(GUILD, "all", 0, 0), (GUILD, "chores", 0, 0)])
    migrations.apply_migrations_sync(db)
    assert _boards(db) == {GUILD: (0, 0)}


def test_guilds_are_merged_independently(db):
    _seed(db, [
        (GUILD, "all", 0, 0),
        (GUILD, "chores", PROD_CHORE_CHANNEL, PROD_CHORE_MESSAGE),
        (OTHER_GUILD, "all", 111, 222),
    ])
    migrations.apply_migrations_sync(db)
    assert _boards(db) == {
        GUILD: (PROD_CHORE_CHANNEL, PROD_CHORE_MESSAGE),
        OTHER_GUILD: (111, 222),
    }


def test_a_guild_with_no_board_row_stays_absent(db):
    migrations.apply_migrations_sync(db)
    assert _boards(db) == {}


def test_guild_id_is_the_whole_key_afterwards(db):
    """A second row per guild is what the merge exists to make impossible."""
    _seed(db, [(GUILD, "all", 111, 222)])
    migrations.apply_migrations_sync(db)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO todo_board (guild_id, channel_id, message_id, updated_at)"
            " VALUES (?, 999, 888, 0)",
            (GUILD,),
        )
