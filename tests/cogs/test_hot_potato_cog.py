"""Cog-runtime tests for Hot Potato (duel): the hand-rolled _explode resolution.

Hot Potato never routes through BaseDuel._finalize_result — _explode writes the
terminal state itself — so its payout needs its own pin.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest_asyncio

from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot

GUILD = 9001
CH = 100
P1, P2 = 1, 2


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _econ_cog(db: GamesDb, db_path: Path) -> HotPotatoDuel:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, db_path, [P1, P2])
    return HotPotatoDuel(bot)  # type: ignore[arg-type]


async def _holding_game(db: GamesDb, holder: int):
    gid = await hpdb.create_game(db, GUILD, CH, P1, P2, None)
    now = time.time()
    log = json.dumps([{"holder_id": holder, "received_at": now - 3.0, "passed_at": None}])
    await hpdb.set_game_state(
        db, gid, "ACTIVE",
        holder_id=holder,
        started_at=now - 10.0,
        timer_seconds=10.0,
        pass_log=log,
        last_action_at=now,
    )
    return await hpdb.get_game(db, gid)


async def test_explode_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _holding_game(db, holder=P2)
    await cog._explode(game.id)
    g = await hpdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == P1
    assert g.loser_id == P2
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25   # participation + win
        assert get_balance(conn, GUILD, P2) == 5    # participation only


async def test_explode_noop_when_already_resolved(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _holding_game(db, holder=P2)
    await hpdb.set_game_state(db, game.id, "RESOLVED", winner_id=P1, loser_id=P2)
    await cog._explode(game.id)
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 0
        assert get_balance(conn, GUILD, P2) == 0
