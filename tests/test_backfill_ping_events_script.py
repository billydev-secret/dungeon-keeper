"""The ping backfill as a script, after the Admin Backfill panel was retired.

The service-layer function is already covered by
tests/test_ping_tracker_logic.py; what is new here is the script wrapper — the
guild sweep, the --since cutoff and the --dry-run rollback. The dry-run test is
the one that matters: ``open_db`` commits on a clean exit, so "don't commit" is
not enough to make a dry run safe, and getting that wrong would write to the
production database while claiming it had not.
"""

from __future__ import annotations

import sqlite3

import pytest

from scripts import backfill_ping_events as script


def _make_db(path) -> str:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (message_id INTEGER, guild_id INTEGER,"
        " channel_id INTEGER, author_id INTEGER, content TEXT, ts REAL)"
    )
    conn.execute(
        "CREATE TABLE known_users (guild_id INTEGER, user_id INTEGER, is_bot INTEGER)"
    )
    # Mirrors migration 198. The schema is owned by migrations, not by the
    # script — the retired route assumed the same, so this keeps parity.
    conn.execute(
        "CREATE TABLE ping_events ("
        " message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,"
        " channel_id INTEGER NOT NULL, author_id INTEGER NOT NULL,"
        " role_ids TEXT NOT NULL DEFAULT '[]', everyone INTEGER NOT NULL DEFAULT 0,"
        " source TEXT NOT NULL DEFAULT 'member', ref TEXT, ts REAL NOT NULL)"
    )
    conn.commit()
    conn.close()
    return str(path)


def _seed(db: str, rows) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
        " content, ts) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _ping_count(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM ping_events").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    return _make_db(tmp_path / "t.db")


def test_all_guilds_sweeps_every_guild_with_messages(db, monkeypatch):
    """A per-guild run silently covers one server; the bot is in several."""
    _seed(db, [
        (1, 100, 7, 42, "hey <@&555> come look", 1_700_000_000.0),
        (2, 200, 8, 43, "@everyone heads up", 1_700_000_100.0),
    ])
    monkeypatch.setattr(
        "sys.argv", ["backfill_ping_events", "--all-guilds", "--db", db]
    )
    script.main()
    assert _ping_count(db) == 2


def test_a_single_guild_run_leaves_the_other_guild_alone(db, monkeypatch):
    _seed(db, [
        (1, 100, 7, 42, "hey <@&555>", 1_700_000_000.0),
        (2, 200, 8, 43, "@everyone", 1_700_000_100.0),
    ])
    monkeypatch.setattr(
        "sys.argv", ["backfill_ping_events", "--guild-id", "100", "--db", db]
    )
    script.main()
    conn = sqlite3.connect(db)
    guilds = [r[0] for r in conn.execute("SELECT DISTINCT guild_id FROM ping_events")]
    conn.close()
    assert guilds == [100]


def test_dry_run_writes_nothing(db, monkeypatch):
    """open_db commits on a clean exit, so the dry run must roll back itself."""
    _seed(db, [(1, 100, 7, 42, "hey <@&555>", 1_700_000_000.0)])
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_ping_events", "--all-guilds", "--db", db, "--dry-run"],
    )
    script.main()
    assert _ping_count(db) == 0


def test_rerunning_records_nothing_new(db, monkeypatch):
    """Keyed on message id — a second pass must be a no-op, not a duplicate."""
    _seed(db, [(1, 100, 7, 42, "hey <@&555>", 1_700_000_000.0)])
    monkeypatch.setattr(
        "sys.argv", ["backfill_ping_events", "--all-guilds", "--db", db]
    )
    script.main()
    first = _ping_count(db)
    script.main()
    assert _ping_count(db) == first == 1


def test_since_skips_messages_before_the_cutoff(db, monkeypatch):
    _seed(db, [
        (1, 100, 7, 42, "old <@&555>", 1_600_000_000.0),
        (2, 100, 7, 42, "new <@&555>", 1_800_000_000.0),
    ])
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_ping_events", "--all-guilds", "--db", db, "--since", "2026-01-01"],
    )
    script.main()
    conn = sqlite3.connect(db)
    ids = [r[0] for r in conn.execute("SELECT message_id FROM ping_events")]
    conn.close()
    assert ids == [2]


def test_a_malformed_since_is_refused_rather_than_silently_ignored(db, monkeypatch):
    """Falling back to 0.0 would scan all history while reporting a cutoff."""
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_ping_events", "--all-guilds", "--db", db, "--since", "last week"],
    )
    with pytest.raises(SystemExit):
        script.main()


def test_guild_id_and_all_guilds_are_mutually_exclusive(db, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_ping_events", "--all-guilds", "--guild-id", "1", "--db", db],
    )
    with pytest.raises(SystemExit):
        script.main()
