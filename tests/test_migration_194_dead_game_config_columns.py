"""Migration 194: eight columns that were schema'd for features nobody built.

Each was a dial with a default, a column and no reader — the shape CLAUDE.md's
"never ship a preference or toggle that isn't enforced" rule exists to stop.
The defect queue took them out of the defaults dicts the panels merge over;
this asserts the columns themselves went too, and that the dials beside them
that *are* live were not caught in the sweep.

``confession_config.max_attachments`` is the one with prod rows, so it is also
the one where the in-code DDL matters: ``confessions_service._create_tables``
must not put the column back on a fresh install.
"""

from __future__ import annotations

import pytest

import migrations
from bot_modules.core.db_utils import open_db

DROPPED = [
    ("duel_config", "allow_early_revert"),
    ("quickdraw_config", "void_on_double_noshow"),
    ("hp_group_config", "shake_threshold"),
    ("hp_group_config", "pass_mode"),
    ("hp_group_config", "lobby_timeout"),
    ("chicken_config", "lobby_timeout"),
    ("mc_config", "lobby_timeout"),
    ("confession_config", "max_attachments"),
]

KEPT = [
    ("duel_config", "nick_denylist"),
    ("quickdraw_config", "draw_window"),
    ("hp_group_config", "min_fuse"),
    ("hp_group_config", "max_players"),
    ("chicken_config", "climb_duration"),
    ("mc_config", "scramble_window"),
    ("confession_config", "max_chars"),
]


def _columns(db_path, table: str) -> set[str]:
    with open_db(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def migrated(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    return db


@pytest.mark.parametrize("table,column", DROPPED)
def test_the_dead_column_is_gone(migrated, table: str, column: str):
    assert column not in _columns(migrated, table)


@pytest.mark.parametrize("table,column", KEPT)
def test_the_live_dial_beside_it_survives(migrated, table: str, column: str):
    assert column in _columns(migrated, table)


def test_the_confessions_ddl_does_not_put_max_attachments_back(migrated):
    """The service builds its own tables at startup. While that DDL still
    named the column, a fresh install would disagree with every live server —
    the drift class ``test_migrations_schema`` exists to catch."""
    from bot_modules.services import confessions_service

    confessions_service.init_db(migrated)
    assert "max_attachments" not in _columns(migrated, "confession_config")


@pytest.mark.asyncio
async def test_the_panel_config_payload_no_longer_offers_the_dropped_dials(migrated):
    """`get_config` merges the stored row over a defaults dict and hands the
    result to the panel. With no row it is pure defaults, so this is where a
    resurrected dial would show up first."""
    from bot_modules.cogs.chicken import db as chicken_db
    from bot_modules.cogs.hot_potato_group import db as hp_db
    from bot_modules.cogs.musical_chairs import db as mc_db
    from bot_modules.cogs.quickdraw import db as qd_db
    from bot_modules.duels.db import _CONFIG_DEFAULTS
    from bot_modules.services.games_db import GamesDb

    db = GamesDb(migrated)
    guild = 42

    assert "allow_early_revert" not in _CONFIG_DEFAULTS

    hp = await hp_db.get_config(db, guild)
    assert {"shake_threshold", "pass_mode", "lobby_timeout"}.isdisjoint(hp)
    assert hp["min_fuse"] == 20.0

    assert "lobby_timeout" not in await chicken_db.get_config(db, guild)
    assert "lobby_timeout" not in await mc_db.get_config(db, guild)
    assert "void_on_double_noshow" not in await qd_db.get_config(db, guild)
