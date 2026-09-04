"""The recap relaunch guard names the game from ``GAME_NAMES`` itself.

The three recap cards (Name Your Price, Mt. Rushmore Draft, Clapback) used to
hand ``relaunch_refusal`` their own display-name string — a fourth copy of a
name the constants table already owns. The guard's branches are pinned in
``tests/cogs/test_games_price_cog.py``; this file pins only the label rule.
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils.game_manager import relaunch_refusal
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 777


async def _closed_dial(db: GamesDb, game_type: str) -> None:
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, 0)",
        (GUILD, game_type),
    )


@pytest.mark.parametrize("game_type", ["price", "rushmore", "clapback"])
async def test_disabled_line_names_the_game_from_the_constants_table(
    sync_db_path, game_type
):
    db = GamesDb(sync_db_path)
    await _closed_dial(db, game_type)
    msg = await relaunch_refusal(db, game_type, CHAN, GUILD)
    assert msg == f"{GAME_NAMES[game_type]} is currently disabled on this server."


async def test_an_explicit_label_still_wins(sync_db_path):
    db = GamesDb(sync_db_path)
    await _closed_dial(db, "price")
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="The Price Game")
    assert msg == "The Price Game is currently disabled on this server."
