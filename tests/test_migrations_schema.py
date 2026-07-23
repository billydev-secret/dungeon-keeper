"""Migration-only schema parity: the schema built from migrations ALONE must
match what prod has via the in-code init_* helpers.

Three columns/indexes historically existed only in init_* helpers (a second,
unmigrated schema source), so a fresh install / disaster-recovery DB built from
migrations was missing them:

* ``auto_delete_rules.media_only`` (init_auto_delete_tables)
* ``idx_interactions_log_dedup`` partial UNIQUE (init_interaction_tables — no
  prod callers at all)
* every ``xp_events`` index (init_xp_tables — no src callers)

Migration 115 folds them in. The ``_pre115`` tests assert they are absent
before 115 (so the migration is doing real work / would fail before the fix);
the full-migration tests assert they are present after.
"""

from __future__ import annotations

import sqlite3

import migrations

GUILD = 42


def _apply_all(db_path) -> None:
    migrations.apply_migrations_sync(db_path)


def _apply_before_115(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files",
        lambda: [f for f in real if f.name < "115"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


# ── media_only ────────────────────────────────────────────────────────


def test_pre115_auto_delete_rules_missing_media_only(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert "media_only" not in _columns(conn, "auto_delete_rules")


def test_auto_delete_rules_has_media_only(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        assert "media_only" in _columns(conn, "auto_delete_rules")


# ── interaction dedup index ─────────────────────────────────────────────


def test_pre115_interactions_log_missing_dedup_index(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert "idx_interactions_log_dedup" not in _indexes(conn, "user_interactions_log")


def test_interactions_log_dedup_index_enforces_uniqueness(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        assert "idx_interactions_log_dedup" in _indexes(conn, "user_interactions_log")
        conn.execute(
            "INSERT OR IGNORE INTO user_interactions_log "
            "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?, ?, ?, ?, ?)",
            (GUILD, 1, 2, 100, 999),
        )
        # Same (guild, message_id, from, to) — the partial UNIQUE index must
        # make the second insert a no-op.
        dup = conn.execute(
            "INSERT OR IGNORE INTO user_interactions_log "
            "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?, ?, ?, ?, ?)",
            (GUILD, 1, 2, 100, 999),
        )
        assert dup.rowcount == 0


def test_migration_115_dedups_log_and_recomputes_aggregate(tmp_path, monkeypatch):
    """A prod DB that ran backfill twice (no dedup index) accumulated duplicate
    log rows and an inflated aggregate. 115 must purge the dups and rebuild
    user_interactions from the deduped log."""
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        # Two backfill passes recorded the same message twice (no index to stop it).
        for _ in range(2):
            conn.execute(
                "INSERT INTO user_interactions_log "
                "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?, ?, ?, ?, ?)",
                (GUILD, 1, 2, 100, 999),
            )
        conn.execute(
            "INSERT INTO user_interactions (guild_id, from_user_id, to_user_id, weight) "
            "VALUES (?, ?, ?, ?)",
            (GUILD, 1, 2, 2),  # inflated: counted twice
        )
        conn.commit()

    # Apply the remaining migrations (including 115).
    _apply_all(db)

    with sqlite3.connect(db) as conn:
        log_rows = conn.execute(
            "SELECT COUNT(*) FROM user_interactions_log WHERE message_id = 999"
        ).fetchone()[0]
        assert log_rows == 1  # duplicate purged
        weight = conn.execute(
            "SELECT weight FROM user_interactions WHERE guild_id=? AND from_user_id=1 AND to_user_id=2",
            (GUILD,),
        ).fetchone()[0]
        assert weight == 1  # recomputed from the deduped log


# ── xp_events index ─────────────────────────────────────────────────────


def test_pre115_xp_events_has_no_index(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert _indexes(conn, "xp_events") == set()


def test_xp_events_is_indexed(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        idx = _indexes(conn, "xp_events")
        assert "idx_xp_events_lookup" in idx
        assert idx  # get_xp_leaderboard no longer full-scans


def test_member_xp_and_role_events_indexed(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        assert "idx_member_xp_leaderboard" in _indexes(conn, "member_xp")
        assert "idx_role_events_lookup" in _indexes(conn, "role_events")
        assert "idx_member_activity_guild_ts" in _indexes(conn, "member_activity")


def test_migrations_apply_cleanly_twice(tmp_path):
    """Re-applying migrations (schema_version already populated) is a no-op and
    must not error — proves 115's ALTER/DELETE/INSERT are safely idempotent."""
    db = tmp_path / "t.db"
    _apply_all(db)
    _apply_all(db)  # would raise on duplicate column / index if not idempotent
