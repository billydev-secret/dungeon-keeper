"""Migration 189: the partners table and two orphaned columns stay gone.

The 2026-08-28 readiness review cut the never-used partners system (0 rows
all-time, no downstream reader, and a request path that predated the
no-contact rule) and two write-only columns the 2026-07-30 honesty pass had
already orphaned (wellness_users.cooldown_until,
wellness_config.crisis_resource_url).

The interesting case is the second test: the web server bootstraps the
wellness schema via ``init_wellness_tables`` WITHOUT applying migrations
(server.py), so a DB first touched by the dashboard must still survive the
chain — a plain ``ALTER TABLE ... DROP COLUMN`` aborts with "no such column"
if the service DDL ever stops creating the legacy columns. That is why
``init_wellness_tables`` deliberately still creates both.
"""

from __future__ import annotations

import sqlite3

import migrations
from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_service import init_wellness_tables


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _assert_dropped(db_path) -> None:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wellness_partners'"
        ).fetchone()
        assert row is None
        users = _columns(conn, "wellness_users")
        assert "cooldown_until" not in users
        assert "paused_until" in users  # its live neighbor survives
        config = _columns(conn, "wellness_config")
        assert "crisis_resource_url" not in config
        assert "default_enforcement" in config


def test_full_chain_drops_table_and_columns(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _assert_dropped(db)


def test_chain_survives_a_service_ddl_bootstrapped_db(tmp_path):
    """A DB first created by the web server (init_wellness_tables, no
    migrations) must apply the whole chain afterwards without 189's ALTERs
    aborting on a column that was never created."""
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        init_wellness_tables(conn)
    migrations.apply_migrations_sync(db)
    _assert_dropped(db)
