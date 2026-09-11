"""Migration 220: the retired player-limit dials stranded in games_game_config.

Clapback's Minimum/Maximum Players dials were retired on 2026-08-27 — the cog
caps its lobby with module constants and never read the stored pair — but the
row they had written survived, and until 37e3f351 the config PUT merged into
the stored options rather than replacing them, so every later save carried the
dead keys forward. The merge is fixed; this migration deletes what it stranded.

The hazard is the one migration 217 recorded in another shape: these two key
names are *not* dead everywhere. Most Likely To and Mt. Rushmore Draft both
have a join phase, still offer both dials and still read them, so a blanket
json_remove over the table would delete two live settings for two games. The
migration enumerates game types instead of matching on key name, and the third
test here is what bites if someone later "simplifies" it into a key match.
"""

from __future__ import annotations

import json

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666
OTHER_GUILD = 1476525656115515484

DEAD = ("min_players", "max_players")

#: The live prod row, read back read-only on 2026-09-11.
PROD_CLAPBACK = {"min_players": 2, "max_players": 16}

#: Its only neighbour in the table. Photo Challenge owns this row from its own
#: panel and must come through untouched.
PROD_PHOTO = {"channel_id": "1528057071235371088", "ping_role_id": ""}


def _seed(db_path, rows) -> None:
    """Insert (guild_id, game_type, options) rows and re-arm migration 220."""
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO games_game_config (guild_id, game_type, options)"
            " VALUES (?, ?, ?)",
            [(g, gt, json.dumps(opts)) for g, gt, opts in rows],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '220%'")
        conn.commit()


def _options(db_path, guild_id, game_type) -> dict:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT options FROM games_game_config WHERE guild_id = ? AND game_type = ?",
            (guild_id, game_type),
        ).fetchone()
    return json.loads(row[0])


def test_the_stranded_clapback_pair_is_removed(tmp_path):
    """The live row becomes an empty bag, not a bag of zeroes: a stored 0 still
    reads as a limit somebody chose."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(GUILD, "clapback", PROD_CLAPBACK)])
    migrations.apply_migrations_sync(db)

    assert _options(db, GUILD, "clapback") == {}


def test_it_leaves_the_other_dials_on_the_same_row_alone(tmp_path):
    """Clapback's live dials ride in the same JSON bag as the dead pair."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    live = {"rounds": 7, "timer": 90, "vote_timer": 30, "anonymous": True, "tags": "spicy"}
    _seed(db, [(GUILD, "clapback", {**PROD_CLAPBACK, **live})])
    migrations.apply_migrations_sync(db)

    assert _options(db, GUILD, "clapback") == live


def test_it_does_not_touch_the_two_games_whose_limits_are_live(tmp_path):
    """MLT and Rushmore have a lobby, offer both dials and read them
    (games_mlt_cog.py:519, games_rushmore_cog.py:878). A key-name match instead
    of a game-type match would silently unset two real settings — the same
    prefix-matching trap migration 217 recorded. Photo Challenge's row is here
    for the same reason: it is the only other row in prod."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    limits = {"min_players": 4, "max_players": 12}
    _seed(db, [
        (GUILD, "clapback", PROD_CLAPBACK),
        (GUILD, "mlt", limits),
        (GUILD, "rushmore", {**limits, "mode": "snake"}),
        (GUILD, "photo", PROD_PHOTO),
    ])
    migrations.apply_migrations_sync(db)

    assert _options(db, GUILD, "mlt") == limits
    assert _options(db, GUILD, "rushmore") == {**limits, "mode": "snake"}
    assert _options(db, GUILD, "photo") == PROD_PHOTO
    assert _options(db, GUILD, "clapback") == {}


def test_it_clears_the_pair_in_every_guild(tmp_path):
    """Only the home guild carries the row today, but a second guild's copy is
    no more alive. The other four names are RETIRED's remaining holders of this
    pair; prod has no row for any of them."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [
        (guild, game_type, PROD_CLAPBACK)
        for guild in (0, GUILD, OTHER_GUILD)
        for game_type in ("clapback", "wyr", "ama", "nhie", "price")
    ])
    migrations.apply_migrations_sync(db)

    for guild in (0, GUILD, OTHER_GUILD):
        for game_type in ("clapback", "wyr", "ama", "nhie", "price"):
            assert _options(db, guild, game_type) == {}, (guild, game_type)


def test_a_row_without_the_pair_is_left_untouched(tmp_path):
    """Re-running migrations must not churn rows that were already clean."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(GUILD, "clapback", {"rounds": 5})])
    migrations.apply_migrations_sync(db)

    assert _options(db, GUILD, "clapback") == {"rounds": 5}
