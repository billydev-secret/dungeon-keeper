"""Integration tests for hot_potato_group/db.py using GamesDb + real SQLite."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.duels import db as duels_db
from bot_modules.services.games_db import GamesDb


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


GUILD = 9001
CH = 100
HOST = 10


# ── create / get ──────────────────────────────────────────────────────────────

async def test_create_lobby_initial_state(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    game = await hpgdb.get_game(db, gid)
    assert game is not None
    assert game.state == "LOBBY"
    assert game.host_id == HOST
    assert game.roster == [HOST]
    assert game.alive == []
    assert game.elimination_order == []


async def test_create_lobby_stores_stakes(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, "loser sings")
    game = await hpgdb.get_game(db, gid)
    assert game.stakes_text == "loser sings"


async def test_get_missing_returns_none(db):
    assert await hpgdb.get_game(db, 99999) is None


# ── set_game_state with JSON columns ───────────────────────────────────────────

async def test_set_state_roundtrips_roster_alive_elim(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(
        db, gid, "ACTIVE",
        roster=json.dumps([10, 20, 30]),
        alive=json.dumps([20, 30]),
        elimination_order=json.dumps([10]),
        holder_id=20,
        round=2,
    )
    game = await hpgdb.get_game(db, gid)
    assert game.state == "ACTIVE"
    assert game.roster == [10, 20, 30]
    assert game.alive == [20, 30]
    assert game.elimination_order == [10]
    assert game.holder_id == 20
    assert game.round == 2


async def test_set_state_resolved_winner_loser(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(db, gid, "RESOLVED", winner_id=20, loser_id=10, resolved_at=time.time())
    game = await hpgdb.get_game(db, gid)
    assert game.state == "RESOLVED"
    assert game.winner_id == 20
    assert game.loser_id == 10


# ── fetch helpers ──────────────────────────────────────────────────────────────

async def test_fetch_lobby_games(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    games = await hpgdb.fetch_lobby_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_active_games(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(db, gid, "ACTIVE")
    games = await hpgdb.fetch_active_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_resolved_games(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(db, gid, "RESOLVED")
    games = await hpgdb.fetch_resolved_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_stale_lobby(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(db, gid, "LOBBY", last_action_at=time.time() - 200)
    games = await hpgdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_excludes_fresh_active(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(db, gid, "ACTIVE", last_action_at=time.time())
    games = await hpgdb.fetch_sweepable_games(db, time.time())
    assert not any(g.id == gid for g in games)


# ── config ────────────────────────────────────────────────────────────────────

async def test_get_config_defaults(db):
    cfg = await hpgdb.get_config(db, GUILD)
    assert cfg["min_fuse"] == pytest.approx(20.0)
    assert cfg["max_fuse"] == pytest.approx(60.0)
    assert cfg["min_players"] == 2
    assert cfg["max_players"] == 10


async def test_upsert_config_updates(db):
    await hpgdb.upsert_config(db, GUILD, min_players=4, max_fuse=90.0)
    cfg = await hpgdb.get_config(db, GUILD)
    assert cfg["min_players"] == 4
    assert cfg["max_fuse"] == pytest.approx(90.0)
    assert cfg["min_fuse"] == pytest.approx(20.0)  # untouched


# ── stats ─────────────────────────────────────────────────────────────────────

async def test_get_stats_empty(db):
    assert await hpgdb.get_stats(db, GUILD, 99) == {"wins": 0, "losses": 0, "total_games": 0}


async def test_get_stats_counts_membership(db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, HOST, None)
    await hpgdb.set_game_state(
        db, gid, "RESOLVED",
        roster=json.dumps([10, 20, 30]),
        winner_id=20, loser_id=10,
    )
    s_host = await hpgdb.get_stats(db, GUILD, 10)
    assert s_host == {"wins": 0, "losses": 1, "total_games": 1}
    s_win = await hpgdb.get_stats(db, GUILD, 20)
    assert s_win == {"wins": 1, "losses": 0, "total_games": 1}
    s_other = await hpgdb.get_stats(db, GUILD, 30)
    assert s_other == {"wins": 0, "losses": 0, "total_games": 1}
    s_outsider = await hpgdb.get_stats(db, GUILD, 999)
    assert s_outsider["total_games"] == 0


# ── group cooldowns (shared duels/db.py) ───────────────────────────────────────

async def test_group_cooldown_set_and_check(db):
    assert await duels_db.check_group_cooldown(db, GUILD, "hot_potato_group", HOST, 48) is None
    await duels_db.set_group_cooldown(db, GUILD, "hot_potato_group", HOST)
    remaining = await duels_db.check_group_cooldown(db, GUILD, "hot_potato_group", HOST, 48)
    assert remaining is not None and remaining > 0


async def test_group_cooldown_zero_hours_disabled(db):
    await duels_db.set_group_cooldown(db, GUILD, "hot_potato_group", HOST)
    assert await duels_db.check_group_cooldown(db, GUILD, "hot_potato_group", HOST, 0) is None
