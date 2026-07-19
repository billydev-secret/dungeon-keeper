"""Migration 090 round-trip: the econ_rentals rebuild that retires gift_color.

Seeds a pre-090 database (migrations up to 089 only), inserts gift_color
rentals alongside the self-rental that could collide with the rewrite, then
applies 090 and asserts the copy: gift_color rows land as role_color with
beneficiary/state intact, the live-rental unique index survives, the widened
perk CHECK accepts the round-2 kinds, and econ_streaks grew ``shields``.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 1
T0 = 1_000_000.0


def _apply_up_to(db_path, monkeypatch, cutoff: str) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files",
        lambda: [f for f in real if f.name < cutoff],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _insert_rental(
    conn: sqlite3.Connection,
    user_id: int,
    perk: str,
    beneficiary_id: int,
    state: str = "active",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO econ_rentals
            (guild_id, user_id, perk, state, price, started_at, next_bill_at,
             cancel_at_period_end, suspended, beneficiary_id, created_at)
        VALUES (?, ?, ?, ?, 50, ?, ?, 0, 0, ?, ?)
        """,
        (GUILD, user_id, perk, state, T0, T0 + 7 * 86400, beneficiary_id, T0),
    )
    return int(cur.lastrowid or 0)


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """A DB seeded pre-090 with gift_color rows, then migrated through 090."""
    db_path = tmp_path / "t.db"
    _apply_up_to(db_path, monkeypatch, "090")

    with sqlite3.connect(db_path) as conn:
        # Friend 2 self-rents a color AND receives a gifted color from 1 —
        # after the rewrite both are role_color; the live index must not
        # collide because the owner differs.
        self_id = _insert_rental(conn, 2, "role_color", 2)
        gift_id = _insert_rental(conn, 1, "gift_color", 2)
        dead_id = _insert_rental(conn, 3, "gift_color", 4, state="lapsed")
        conn.execute(
            "INSERT INTO econ_streaks (guild_id, user_id, current_streak, "
            "longest_streak) VALUES (?, 9, 3, 5)",
            (GUILD,),
        )
        conn.commit()

    migrations.apply_migrations_sync(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn, {"self": self_id, "gift": gift_id, "dead": dead_id}
    conn.close()


def test_gift_color_rows_rewritten_to_role_color(migrated):
    conn, ids = migrated
    assert conn.execute(
        "SELECT COUNT(*) FROM econ_rentals WHERE perk = 'gift_color'"
    ).fetchone()[0] == 0
    gift = conn.execute(
        "SELECT * FROM econ_rentals WHERE id = ?", (ids["gift"],)
    ).fetchone()
    assert gift["perk"] == "role_color"
    assert gift["user_id"] == 1 and gift["beneficiary_id"] == 2
    assert gift["state"] == "active"
    # Historical rows are rewritten too, state intact.
    dead = conn.execute(
        "SELECT * FROM econ_rentals WHERE id = ?", (ids["dead"],)
    ).fetchone()
    assert dead["perk"] == "role_color" and dead["state"] == "lapsed"


def test_live_unique_index_survives_rebuild(migrated):
    conn, _ids = migrated
    # Same (guild, user, perk, beneficiary) as the rewritten gift → collision.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_rental(conn, 1, "role_color", 2)


def test_widened_check_accepts_round2_kinds_rejects_gift_color(migrated):
    conn, _ids = migrated
    _insert_rental(conn, 5, "voice_style", 5)
    _insert_rental(conn, 6, "emoji", 6)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_rental(conn, 7, "gift_color", 8)


def test_streaks_gained_shields_default_zero(migrated):
    conn, _ids = migrated
    row = conn.execute(
        "SELECT shields, current_streak FROM econ_streaks WHERE user_id = 9"
    ).fetchone()
    assert row["shields"] == 0
    assert row["current_streak"] == 3
