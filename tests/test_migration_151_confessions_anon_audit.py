"""Migration 151: backfill `confession_threads` into `anon_audit_log`.

The Confessions audit panel moved off `confession_threads` (a seven-day
operational cache) onto `anon_audit_log`, so without this backfill the panel
reads empty on the day it ships even though up to a week of confessions is
sitting in the operational table.

The SQL carries real logic worth pinning: a CASE that classifies each row as a
root confession or a reply by comparing `root_message_id` to `message_id`, and
a correlated subquery that recovers a reply's `target_id` from its root row.
Invert either and every backfilled row is mislabelled in the panel with nothing
failing — `test_migrations_schema.py` only proves the file parses and runs.

Prod at the time of writing: 24 rows across two guilds (5 + 17 in the main
guild, 1 + 1 in the other), all on text-channel destinations.
"""

from __future__ import annotations

import json
import sqlite3

import migrations

GUILD = 1469491362444480666
OTHER_GUILD = 1185374510594138222
CHANNEL = 1469771843320811602
OP = 416829953355808808
REPLIER = 884813145833619466

ROOT_MSG = 1533397048785895607
REPLY_MSG = 1533542557777137877
ORPHAN_REPLY_MSG = 1533542557777137999


def _apply_before_151(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files",
        lambda: [f for f in real if f.name < "151"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _seed_prod_shape(db_path) -> None:
    """A confession, a reply to it, and a reply whose root has aged out."""
    conn = sqlite3.connect(db_path)
    rows = [
        # (guild, message, channel, root, author, created_at)
        (GUILD, ROOT_MSG, CHANNEL, ROOT_MSG, OP, 1785000000),
        (GUILD, REPLY_MSG, CHANNEL, ROOT_MSG, REPLIER, 1785000100),
        # Root purged already — target_id is unrecoverable, which is not the
        # same as "this reply was addressed to nobody".
        (GUILD, ORPHAN_REPLY_MSG, CHANNEL, 999999999999, REPLIER, 1785000200),
        # A different guild's confession must not bleed across.
        (OTHER_GUILD, 1533499017739239424, 100, 1533499017739239424, 42, 1785000300),
    ]
    conn.executemany(
        "INSERT INTO confession_threads (guild_id, message_id, channel_id,"
        " root_message_id, original_author_id, notify_original_author, created_at)"
        " VALUES (?, ?, ?, ?, ?, 1, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _audit(db_path, guild_id: int = GUILD) -> dict[int, sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM anon_audit_log WHERE feature = 'confessions' AND guild_id = ?",
        (guild_id,),
    ).fetchall()
    conn.close()
    return {int(r["message_id"]): r for r in rows}


def test_root_and_reply_are_classified_apart(tmp_path, monkeypatch):
    db = tmp_path / "backfill.db"
    _apply_before_151(db, monkeypatch)
    _seed_prod_shape(db)
    assert _audit(db) == {}

    migrations.apply_migrations_sync(db)

    rows = _audit(db)
    assert rows[ROOT_MSG]["event"] == "confession_posted"
    assert rows[REPLY_MSG]["event"] == "reply_posted"
    assert rows[ROOT_MSG]["actor_id"] == OP
    assert rows[REPLY_MSG]["actor_id"] == REPLIER


def test_reply_target_resolves_to_the_root_author(tmp_path, monkeypatch):
    db = tmp_path / "target.db"
    _apply_before_151(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)

    rows = _audit(db)
    assert rows[REPLY_MSG]["target_id"] == OP
    # A root confession is addressed to nobody.
    assert rows[ROOT_MSG]["target_id"] is None
    # Root already purged: unknown, recorded as NULL rather than invented.
    assert rows[ORPHAN_REPLY_MSG]["target_id"] is None


def test_root_message_id_is_stored_as_a_string(tmp_path, monkeypatch):
    """A bare snowflake past 2^53 loses precision in the dashboard's JS."""
    db = tmp_path / "snowflake.db"
    _apply_before_151(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)

    extra = json.loads(_audit(db)[REPLY_MSG]["extra"])
    assert extra["root_message_id"] == str(ROOT_MSG)
    assert isinstance(extra["root_message_id"], str)


def test_created_at_carries_across_for_the_retention_clock(tmp_path, monkeypatch):
    """Backfilled rows must expire on the same clock as new ones."""
    db = tmp_path / "clock.db"
    _apply_before_151(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)

    assert _audit(db)[ROOT_MSG]["created_at"] == 1785000000


def test_guilds_do_not_bleed(tmp_path, monkeypatch):
    db = tmp_path / "guilds.db"
    _apply_before_151(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)

    assert len(_audit(db)) == 3
    assert len(_audit(db, OTHER_GUILD)) == 1


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    """The NOT EXISTS guard holds if the statement is ever re-applied."""
    db = tmp_path / "idem.db"
    _apply_before_151(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)
    once = len(_audit(db))

    sql = (migrations._MIGRATIONS_DIR / "151_confessions_anon_audit.sql").read_text(
        encoding="utf-8"
    )
    conn = sqlite3.connect(db)
    conn.executescript(sql)
    conn.commit()
    conn.close()

    assert len(_audit(db)) == once
