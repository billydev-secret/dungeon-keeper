"""Migration 164: the casino_mines_hands live-hand table.

Asserting the table exists is not the interesting part — the partial unique
index is, because that is what actually enforces the money-safety rule the
service layer's pre-check only *asks* for: a member must never hold two live
grids, one of which nothing would ever resolve or refund. So this proves both
directions with real INSERTs rather than by reading DDL — a second live grid
is refused, and a second grid whose predecessor has settled is allowed.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 800
CHAN = 9100
A, B = 3001, 3002
NOW = 1_800_000_000.0


def _apply_before_164(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "164"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _insert(conn, *, user_id: int, settled: float | None = None) -> None:
    conn.execute(
        "INSERT INTO casino_mines_hands (guild_id, channel_id, user_id, stake, "
        "bombs, state_json, created_at, last_action_at, settled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (GUILD, CHAN, user_id, 20, 3, '{"bombs": [1], "revealed": []}',
         NOW, NOW, settled),
    )


def test_migration_creates_the_table(tmp_path, monkeypatch):
    db = tmp_path / "mines.db"
    _apply_before_164(db, monkeypatch)
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("casino_mines_hands",),
    ).fetchone() is None
    conn.close()

    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(casino_mines_hands)")}
    assert {
        "id", "guild_id", "channel_id", "message_id", "user_id", "stake",
        "bombs", "state_json", "outcome", "created_at", "last_action_at",
        "settled_at",
    } <= cols
    conn.close()


def test_one_live_grid_per_member_is_enforced_by_the_index(tmp_path):
    db = tmp_path / "mines.db"
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    _insert(conn, user_id=A)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, user_id=A)
    # Another member is unaffected — the index is per (guild, user).
    _insert(conn, user_id=B)
    conn.close()


def test_a_settled_grid_frees_the_member_to_open_another(tmp_path):
    db = tmp_path / "mines.db"
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    _insert(conn, user_id=A, settled=NOW)
    _insert(conn, user_id=A, settled=NOW + 1)
    _insert(conn, user_id=A)  # one live grid on top of two finished ones
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, user_id=A)
    conn.close()
