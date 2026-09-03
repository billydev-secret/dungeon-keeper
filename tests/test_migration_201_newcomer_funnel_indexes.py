"""Migration 201: the (guild_id, author_id, ts) and (guild_id, reply_to_id)
indexes on ``messages`` that ``compute_newcomer_funnel`` depends on.

``compute_newcomer_funnel`` (bot_modules/services/health_metrics.py) runs four
queries *per recent joiner* — "this user's messages since they joined",
"who replied to one of them", "how many distinct channels", "were they active
7 days later". Before this migration the only relevant indexes were
``(guild_id, ts)`` and ``(guild_id, author_id)`` — author_id without ts, and
nothing at all on reply_to_id — so every one of those per-joiner queries
degraded to a guild-wide ts-range scan with the real filter applied as a
residual predicate. Measured on a scratch copy of the prod messages table
(602,710 rows for the guild used), the whole newcomer-funnel loop for that
guild's 197 recent joiners went from 37.4s to 0.18s after these two indexes.

Asserting "the index exists" alone would not catch a column-order slip, so
this pins the query *plans* the way 153's index test does: the newcomer-funnel
queries must seek on the new indexes with the relevant column as a range/
equality constraint, not fall back to a guild-wide scan.
"""

from __future__ import annotations

import sqlite3

import migrations

GUILD = 1469491362444480666

# Verbatim from compute_newcomer_funnel's per-joiner loop shape.
AUTHOR_SINCE_TS = (
    "SELECT MIN(ts) AS first_ts FROM messages "
    "WHERE guild_id=? AND author_id=? AND ts>=?"
)
REPLY_TO_AUTHORS_MESSAGES = """
    SELECT MIN(m.ts) AS reply_ts FROM messages m
    WHERE m.guild_id=? AND m.reply_to_id IN (
        SELECT message_id FROM messages WHERE guild_id=? AND author_id=? AND ts>=?
    ) AND m.author_id != ? AND m.ts>=?
"""

AUTHOR_TS_INDEX = "idx_messages_author_ts"
REPLY_TO_INDEX = "idx_messages_reply_to"


def _apply_before_201(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "201"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _indexes(db_path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'messages'"
    ).fetchall()
    conn.close()
    return {name: sql for name, sql in rows}


def _seed(db_path) -> None:
    # A handful of rows, including a reply chain, so the planner has
    # something to reason about and the reply query has a real match.
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT OR IGNORE INTO messages "
        "(message_id, guild_id, channel_id, author_id, ts) VALUES (?, ?, 1, ?, ?)",
        [(i, GUILD, 100 + i, 1785000000 + i) for i in range(50)],
    )
    conn.execute(
        "INSERT OR IGNORE INTO messages (message_id, guild_id, channel_id, "
        "author_id, reply_to_id, ts) VALUES (999, ?, 1, 200, 0, 1785000100)",
        (GUILD,),
    )
    conn.commit()
    conn.close()


def _plan(db_path, sql: str, params: tuple) -> str:
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    plan = " ".join(
        str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    conn.close()
    return plan


def test_indexes_are_absent_before_the_migration(tmp_path, monkeypatch):
    db = tmp_path / "before.db"
    _apply_before_201(db, monkeypatch)
    idx = _indexes(db)
    assert AUTHOR_TS_INDEX not in idx
    assert REPLY_TO_INDEX not in idx


def test_indexes_exist_after_the_migration(tmp_path):
    db = tmp_path / "after.db"
    migrations.apply_migrations_sync(db)
    idx = _indexes(db)
    author_ts_sql = " ".join(idx[AUTHOR_TS_INDEX].split()).lower()
    reply_to_sql = " ".join(idx[REPLY_TO_INDEX].split()).lower()
    # Column order is the whole point: author_id must lead ts (equality then
    # range), and reply_to_id must be indexed at all.
    assert "messages (guild_id, author_id, ts)" in author_ts_sql
    assert "messages (guild_id, reply_to_id)" in reply_to_sql


def test_author_since_ts_query_uses_the_new_index(tmp_path, monkeypatch):
    db = tmp_path / "author.db"
    _apply_before_201(db, monkeypatch)
    before = _plan(db, AUTHOR_SINCE_TS, (GUILD, 200, 0))
    assert AUTHOR_TS_INDEX not in before

    migrations.apply_migrations_sync(db)
    after = _plan(db, AUTHOR_SINCE_TS, (GUILD, 200, 0))
    assert AUTHOR_TS_INDEX in after
    # ts must be a range constraint alongside the author_id equality, not a
    # post-filter over every one of the guild's messages.
    plan_no_space = after.replace(" ", "")
    assert "author_id=?" in plan_no_space
    assert "ts>?" in plan_no_space


def test_first_reply_query_uses_the_new_reply_index(tmp_path, monkeypatch):
    db = tmp_path / "reply.db"
    params = (GUILD, GUILD, 200, 0, 200, 0)
    _apply_before_201(db, monkeypatch)
    before = _plan(db, REPLY_TO_AUTHORS_MESSAGES, params)
    assert REPLY_TO_INDEX not in before

    migrations.apply_migrations_sync(db)
    after = _plan(db, REPLY_TO_AUTHORS_MESSAGES, params)
    # SQLite flips which side of the IN(subquery) it drives from once
    # reply_to_id is indexed — the outer scan seeks the new index instead of
    # walking every message in the guild's ts range.
    assert REPLY_TO_INDEX in after


def test_migration_is_idempotent(tmp_path):
    """IF NOT EXISTS holds if the file is ever re-applied to a live DB."""
    db = tmp_path / "idem.db"
    migrations.apply_migrations_sync(db)
    sql = (
        migrations._MIGRATIONS_DIR / "201_newcomer_funnel_indexes.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.executescript(sql)
    conn.commit()
    conn.close()
    idx = _indexes(db)
    assert AUTHOR_TS_INDEX in idx
    assert REPLY_TO_INDEX in idx
