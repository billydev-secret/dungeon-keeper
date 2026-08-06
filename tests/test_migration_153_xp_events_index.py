"""Migration 153: the (guild_id, created_at) index on xp_events.

The dashboard landing page runs two ``WHERE guild_id = ? AND created_at >= ?``
aggregates over ``xp_events`` on every load (``routes/home.py``: 24 h XP total
and distinct earners). ``created_at`` sat behind another column in both
pre-existing indexes — ``(guild_id, source, created_at, user_id)`` and
``(guild_id, channel_id, created_at)`` — so SQLite could only seek on
``guild_id`` and then walked every row for the guild (~1.02 M in prod).

Asserting "the index exists" alone would not catch a column-order slip, so this
pins the query *plans*: both landing-page queries must seek on the new index
with ``created_at`` as a range constraint, which is only possible if the index
leads with ``(guild_id, created_at)``.
"""

from __future__ import annotations

import sqlite3

import migrations

GUILD = 1469491362444480666

# Verbatim from routes/home.py's XP block — if those queries change shape, this
# test should be revisited rather than quietly still passing.
XP_TODAY = (
    "SELECT COALESCE(SUM(amount), 0) FROM xp_events "
    "WHERE guild_id = ? AND created_at >= ?"
)
XP_USERS_TODAY = (
    "SELECT COUNT(DISTINCT user_id) FROM xp_events "
    "WHERE guild_id = ? AND created_at >= ?"
)

INDEX = "idx_xp_events_guild_created"


def _apply_before_153(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "153"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _indexes(db_path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'xp_events'"
    ).fetchall()
    conn.close()
    return {name: sql for name, sql in rows}


def _plan(db_path, sql: str) -> str:
    conn = sqlite3.connect(db_path)
    # A few rows so the planner has something to reason about; the plan for
    # this shape does not depend on cardinality, but an empty table makes the
    # test read as vacuous.
    conn.executemany(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at) "
        "VALUES (?, ?, 'message', 1.0, ?)",
        [(GUILD, 100 + i, 1785000000 + i) for i in range(50)],
    )
    conn.commit()
    plan = " ".join(
        str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, (GUILD, 0))
    )
    conn.close()
    return plan


def test_index_is_absent_before_the_migration(tmp_path, monkeypatch):
    db = tmp_path / "before.db"
    _apply_before_153(db, monkeypatch)
    assert INDEX not in _indexes(db)


def test_index_exists_after_the_migration(tmp_path):
    db = tmp_path / "after.db"
    migrations.apply_migrations_sync(db)
    sql = _indexes(db)[INDEX]
    normalized = " ".join(sql.split()).lower()
    # Column order is the whole point: (created_at, guild_id) would not serve
    # a guild-scoped range scan.
    assert "xp_events (guild_id, created_at)" in normalized


def test_xp_total_query_uses_the_new_index(tmp_path, monkeypatch):
    db = tmp_path / "total.db"
    _apply_before_153(db, monkeypatch)
    before = _plan(db, XP_TODAY)
    assert INDEX not in before

    migrations.apply_migrations_sync(db)
    after = _plan(db, XP_TODAY)
    assert INDEX in after
    # created_at must be a range constraint, not a post-filter.
    assert "created_at>" in after.replace(" ", "")


def test_distinct_earners_query_uses_the_new_index(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    _apply_before_153(db, monkeypatch)
    before = _plan(db, XP_USERS_TODAY)
    assert INDEX not in before

    migrations.apply_migrations_sync(db)
    after = _plan(db, XP_USERS_TODAY)
    assert INDEX in after
    assert "created_at>" in after.replace(" ", "")


def test_migration_is_idempotent(tmp_path):
    """IF NOT EXISTS holds if the file is ever re-applied to a live DB."""
    db = tmp_path / "idem.db"
    migrations.apply_migrations_sync(db)
    sql = (
        migrations._MIGRATIONS_DIR / "153_xp_events_guild_created_index.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.executescript(sql)
    conn.commit()
    conn.close()
    assert INDEX in _indexes(db)
