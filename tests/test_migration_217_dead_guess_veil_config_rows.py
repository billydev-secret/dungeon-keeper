"""Migration 217: the two readerless rows the Guess cleanup left behind.

Migration 216 removed the inactivity nudge's dial and its stored round id by
name — the two keys the deleted code actually mentioned. ``guess_last_nudge_at``
was the loop's own clock and had no reader to grep for, so it only surfaced by
reading the live ``config`` table afterwards. ``veil_role_id`` is older: Veil
was renamed to Guess, and the row survived as a second pointer at the same role
``guess_role_id`` held, which is precisely why nobody noticed it.

The hazard here is prefix matching. ``guess_`` and ``veil_`` both prefix live
settings — ``guess_role_id`` and ``guess_channel_id`` run the game, and
``veil_channel_id`` is deliberately left for its own pass — so the delete
enumerates its two keys and these tests pin that it stays enumerated.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666

DEAD = ("veil_role_id", "guess_last_nudge_at")

# Rows that must outlive the migration. The guess_* pair is the running game's
# configuration; veil_channel_id is dead too but is not this migration's job.
SURVIVORS = {
    "guess_role_id": "1502353154371489844",
    "guess_channel_id": "1502760619269427292",
    "veil_channel_id": "1502760619269427292",
}


def _seed(db_path, rows) -> None:
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            rows,
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '217%'")


def _rows(db_path, guild_id) -> dict[str, str]:
    with open_db(db_path) as conn:
        return {
            key: value
            for key, value in conn.execute(
                "SELECT key, value FROM config WHERE guild_id = ?", (guild_id,)
            )
        }


def test_the_two_dead_rows_are_deleted_not_blanked(tmp_path):
    """A key left at 0 or "" still reads as a value somebody chose."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(GUILD, key, "1502766814315282553") for key in DEAD])
    migrations.apply_migrations_sync(db)

    assert set(_rows(db, GUILD)) & set(DEAD) == set()


def test_it_does_not_take_the_live_guess_config_with_them(tmp_path):
    """``guess_role_id`` is the role the game pings and ``guess_channel_id``
    is where it runs. A ``guess_%`` match would take both and dark the game."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [(GUILD, key, "1") for key in DEAD]
        + [(GUILD, key, value) for key, value in SURVIVORS.items()],
    )
    migrations.apply_migrations_sync(db)

    assert _rows(db, GUILD) | SURVIVORS == _rows(db, GUILD)


def test_it_deletes_the_rows_in_every_guild(tmp_path):
    """Unlike 195's legacy grant block this is not scoped to guild 0: the keys
    are per-guild rows and readerless wherever they sit. Only the home guild
    actually carries them today, but a second guild's copy is no more alive."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(guild, key, "1") for guild in (0, GUILD, 1476525656115515484)
               for key in DEAD])
    migrations.apply_migrations_sync(db)

    for guild in (0, GUILD, 1476525656115515484):
        assert set(_rows(db, guild)) & set(DEAD) == set(), guild
