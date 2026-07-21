"""Integration tests for chicken/db.py using GamesDb + real SQLite."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.services.games_db import GamesDb


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


GUILD = 9001
CH = 100
HOST = 10


async def test_create_lobby_initial(db):
    gid = await chdb.create_lobby(db, GUILD, CH, HOST, None)
    g = await chdb.get_game(db, gid)
    assert g.state == "LOBBY"
    assert g.roster == [HOST]
    assert g.bail_log == []


async def test_set_state_roundtrips_bail_log(db):
    gid = await chdb.create_lobby(db, GUILD, CH, HOST, None)
    bail = [{"player_id": 3, "bail_ts": 1.0, "meter_pct": 42.0}]
    await chdb.set_game_state(
        db, gid, "ACTIVE",
        phase="CLIMBING",
        alive=json.dumps([1, 2]),
        bail_log=json.dumps(bail),
        climb_started_at=1000.0,
        climb_duration=25.0,
    )
    g = await chdb.get_game(db, gid)
    assert g.phase == "CLIMBING"
    assert g.alive == [1, 2]
    assert g.bail_log[0]["meter_pct"] == 42.0
    assert g.climb_duration == pytest.approx(25.0)


async def test_fetch_lobby_active_resolved(db):
    gid = await chdb.create_lobby(db, GUILD, CH, HOST, None)
    assert any(g.id == gid for g in await chdb.fetch_lobby_games(db))
    await chdb.set_game_state(db, gid, "ACTIVE")
    assert any(g.id == gid for g in await chdb.fetch_active_games(db))
    await chdb.set_game_state(db, gid, "RESOLVED")
    assert any(g.id == gid for g in await chdb.fetch_resolved_games(db))


async def test_fetch_sweepable_stale_active(db):
    gid = await chdb.create_lobby(db, GUILD, CH, HOST, None)
    await chdb.set_game_state(db, gid, "ACTIVE", last_action_at=time.time() - 700)
    assert any(g.id == gid for g in await chdb.fetch_sweepable_games(db, time.time()))


async def test_config_defaults_and_upsert(db):
    cfg = await chdb.get_config(db, GUILD)
    assert cfg["climb_duration"] == pytest.approx(25.0)
    assert cfg["min_players"] == 2
    assert cfg["max_players"] == 8
    await chdb.upsert_config(db, GUILD, climb_duration=40.0, max_players=6)
    cfg2 = await chdb.get_config(db, GUILD)
    assert cfg2["climb_duration"] == pytest.approx(40.0)
    assert cfg2["max_players"] == 6
