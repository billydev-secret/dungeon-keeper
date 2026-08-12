"""Migration 158: casino rounds become per-player instead of per-channel.

The five windowed games (roulette, derby, baccarat, dice, keno) each carried a
``UNIQUE (channel_id) WHERE status = 'open'`` index — the communal rule that
one round at a time belonged to the *channel*. Private rounds belong to a
player, so the index has to move to ``(guild_id, user_id)`` or two people could
never have their own round open at once.

Asserting the column exists is not enough: the index swap is what actually
enforces the money-safety rule (a player must never hold two live rounds, one
of which nothing would resolve or refund), so this pins both the removal of the
old constraint and the arrival of the new one, and proves each direction with a
real INSERT rather than by reading DDL.

Pools is deliberately excluded — it is a day-long communal market whose payouts
are pro-rata, so it keeps its channel-scoped index.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 800
CHAN = 9100
A, B = 3001, 3002
NOW = 1_800_000_000.0

# (rounds table, old channel-scoped index, new player-scoped index)
GAMES = [
    ("casino_roulette_rounds", "idx_casino_roulette_open",
     "idx_casino_roulette_open_player"),
    ("casino_race_rounds", "idx_casino_race_open",
     "idx_casino_race_open_player"),
    ("casino_baccarat_rounds", "idx_casino_baccarat_open",
     "idx_casino_baccarat_open_player"),
    ("casino_dice_rounds", "idx_casino_dice_open",
     "idx_casino_dice_open_player"),
    ("casino_keno_rounds", "idx_casino_keno_open",
     "idx_casino_keno_open_player"),
]


def _apply_before_158(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "158"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _indexes(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
        (table,),
    ).fetchall()
    conn.close()
    return {name for (name,) in rows}


def _insert(conn, table: str, *, channel: int, user_id: int) -> None:
    conn.execute(
        f"INSERT INTO {table} "
        "(guild_id, channel_id, user_id, opened_at, closes_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (GUILD, channel, user_id, NOW, NOW + 600),
    )


@pytest.mark.parametrize(("table", "old_index", "new_index"), GAMES)
def test_index_swaps_from_channel_to_player(tmp_path, monkeypatch, table,
                                            old_index, new_index):
    db = tmp_path / f"{table}.db"
    _apply_before_158(db, monkeypatch)
    before = _indexes(db, table)
    assert old_index in before
    assert new_index not in before

    migrations.apply_migrations_sync(db)
    after = _indexes(db, table)
    assert old_index not in after
    assert new_index in after


@pytest.mark.parametrize(("table", "old_index", "new_index"), GAMES)
def test_two_players_may_hold_a_round_at_once(tmp_path, table, old_index,
                                              new_index):
    """The constraint being removed: the old index refused the second player."""
    db = tmp_path / f"{table}-two.db"
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    _insert(conn, table, channel=CHAN, user_id=A)
    _insert(conn, table, channel=CHAN, user_id=B)
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2
    conn.close()


@pytest.mark.parametrize(("table", "old_index", "new_index"), GAMES)
def test_one_player_may_not_hold_two(tmp_path, table, old_index, new_index):
    """The constraint being added — the money-safety half of the swap."""
    db = tmp_path / f"{table}-dupe.db"
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    _insert(conn, table, channel=CHAN, user_id=A)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, table, channel=CHAN + 1, user_id=A)
    conn.close()


@pytest.mark.parametrize(("table", "old_index", "new_index"), GAMES)
def test_settled_rounds_do_not_occupy_the_index(tmp_path, table, old_index,
                                                new_index):
    """Partial index: only 'open' rounds are constrained, so a player can
    keep playing after their round resolves."""
    db = tmp_path / f"{table}-settled.db"
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    _insert(conn, table, channel=CHAN, user_id=A)
    conn.execute(f"UPDATE {table} SET status = 'settled'")
    _insert(conn, table, channel=CHAN, user_id=A)
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2
    conn.close()


def test_existing_rows_survive_with_an_inert_sentinel(tmp_path, monkeypatch):
    """Pre-158 rows get user_id 0. Every one of them is already settled or
    void, so the sentinel never collides and never gets read."""
    db = tmp_path / "existing.db"
    _apply_before_158(db, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO casino_roulette_rounds "
        "(guild_id, channel_id, status, opened_at, closes_at, result) "
        "VALUES (?, ?, 'settled', ?, ?, 7)",
        (GUILD, CHAN, NOW, NOW + 45),
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT user_id, status, result FROM casino_roulette_rounds"
    ).fetchone()
    conn.close()
    assert row == (0, "settled", 7)


def test_pools_keeps_its_channel_scoped_index(tmp_path):
    """Pools shares RoundTables but not this change — a day-long parimutuel
    market is genuinely communal, and per-player rounds would make its
    pro-rata split meaningless."""
    db = tmp_path / "pools.db"
    migrations.apply_migrations_sync(db)
    names = _indexes(db, "casino_pools_rounds")
    assert "idx_casino_pools_open" in names
    assert "idx_casino_pools_open_player" not in names
