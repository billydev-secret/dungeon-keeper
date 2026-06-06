"""Integration tests for musical_chairs/db.py using GamesDb + real SQLite."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.cogs.musical_chairs import db as mcdb
from bot_modules.services.games_db import GamesDb


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


GUILD = 9001
CH = 100
HOST = 10


async def test_create_lobby_initial(db):
    gid = await mcdb.create_lobby(db, GUILD, CH, HOST, None)
    g = await mcdb.get_game(db, gid)
    assert g.state == "LOBBY"
    assert g.roster == [HOST]
    assert g.seated == []
    assert g.phase is None


async def test_set_state_roundtrips_json(db):
    gid = await mcdb.create_lobby(db, GUILD, CH, HOST, None)
    await mcdb.set_game_state(
        db, gid, "ACTIVE",
        phase="SCRAMBLE", round=2, chairs=2,
        alive=json.dumps([10, 20, 30]),
        seated=json.dumps([20]),
        elimination_order=json.dumps([]),
    )
    g = await mcdb.get_game(db, gid)
    assert g.phase == "SCRAMBLE"
    assert g.round == 2
    assert g.chairs == 2
    assert g.alive == [10, 20, 30]
    assert g.seated == [20]


async def test_fetch_lobby_active_resolved(db):
    gid = await mcdb.create_lobby(db, GUILD, CH, HOST, None)
    assert any(g.id == gid for g in await mcdb.fetch_lobby_games(db))
    await mcdb.set_game_state(db, gid, "ACTIVE")
    assert any(g.id == gid for g in await mcdb.fetch_active_games(db))
    await mcdb.set_game_state(db, gid, "RESOLVED")
    assert any(g.id == gid for g in await mcdb.fetch_resolved_games(db))


async def test_fetch_sweepable_stale_active(db):
    gid = await mcdb.create_lobby(db, GUILD, CH, HOST, None)
    await mcdb.set_game_state(db, gid, "ACTIVE", last_action_at=time.time() - 700)
    assert any(g.id == gid for g in await mcdb.fetch_sweepable_games(db, time.time()))


async def test_config_defaults_and_upsert(db):
    cfg = await mcdb.get_config(db, GUILD)
    assert cfg["min_players"] == 3
    assert cfg["scramble_window"] == pytest.approx(8.0)
    await mcdb.upsert_config(db, GUILD, min_players=5, scramble_window=4.0)
    cfg2 = await mcdb.get_config(db, GUILD)
    assert cfg2["min_players"] == 5
    assert cfg2["scramble_window"] == pytest.approx(4.0)
    assert cfg2["max_music"] == pytest.approx(15.0)  # untouched


async def test_stats_membership(db):
    gid = await mcdb.create_lobby(db, GUILD, CH, HOST, None)
    await mcdb.set_game_state(
        db, gid, "RESOLVED",
        roster=json.dumps([10, 20, 30]), winner_id=30, loser_id=20,
    )
    assert (await mcdb.get_stats(db, GUILD, 30))["wins"] == 1
    assert (await mcdb.get_stats(db, GUILD, 20))["losses"] == 1
    assert (await mcdb.get_stats(db, GUILD, 10))["total_games"] == 1
    assert (await mcdb.get_stats(db, GUILD, 999))["total_games"] == 0
