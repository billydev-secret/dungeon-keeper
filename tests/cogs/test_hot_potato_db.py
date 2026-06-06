"""Integration tests for hot_potato/db.py using GamesDb + real SQLite."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.services.games_db import GamesDb
from bot_modules.cogs.hot_potato import db as hpdb


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


GUILD = 9001
CH = 100


async def _create(db, **kwargs) -> int:
    defaults = dict(
        guild_id=GUILD,
        channel_id=CH,
        challenger_id=1,
        target_id=2,
        stakes_text=None,
    )
    defaults.update(kwargs)
    return await hpdb.create_game(db, **defaults)


# ── create / get ──────────────────────────────────────────────────────────────

async def test_create_and_get_game(db):
    gid = await _create(db)
    game = await hpdb.get_game(db, gid)
    assert game is not None
    assert game.id == gid
    assert game.state == "PENDING"
    assert game.challenger_id == 1
    assert game.target_id == 2
    assert game.holder_id is None
    assert game.pass_log == []


async def test_get_game_missing_returns_none(db):
    assert await hpdb.get_game(db, 99999) is None


async def test_create_stores_stakes_text(db):
    gid = await _create(db, stakes_text="loser buys dinner")
    game = await hpdb.get_game(db, gid)
    assert game.stakes_text == "loser buys dinner"


# ── set_game_state ─────────────────────────────────────────────────────────────

async def test_set_game_state_active_with_holder(db):
    gid = await _create(db)
    now = time.time()
    log = json.dumps([{"holder_id": 1, "received_at": now, "passed_at": None}])
    await hpdb.set_game_state(
        db, gid, "ACTIVE",
        holder_id=1, timer_seconds=30.0, started_at=now, pass_log=log, last_action_at=now
    )
    game = await hpdb.get_game(db, gid)
    assert game.state == "ACTIVE"
    assert game.holder_id == 1
    assert game.timer_seconds == pytest.approx(30.0)
    assert game.started_at == pytest.approx(now, abs=1)
    assert len(game.pass_log) == 1
    assert game.pass_log[0]["holder_id"] == 1


async def test_set_game_state_resolved_sets_winner_loser(db):
    gid = await _create(db)
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "RESOLVED",
        winner_id=2, loser_id=1, resolved_at=now
    )
    game = await hpdb.get_game(db, gid)
    assert game.state == "RESOLVED"
    assert game.winner_id == 2
    assert game.loser_id == 1


async def test_set_game_state_void(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "VOID")
    game = await hpdb.get_game(db, gid)
    assert game.state == "VOID"


# ── get_active_game_for_pair ──────────────────────────────────────────────────

async def test_get_active_game_for_pair_found(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "ACTIVE")
    result = await hpdb.get_active_game_for_pair(db, GUILD, 1, 2)
    assert result is not None
    assert result.id == gid


async def test_get_active_game_for_pair_reversed_order(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "ACTIVE")
    result = await hpdb.get_active_game_for_pair(db, GUILD, 2, 1)
    assert result is not None
    assert result.id == gid


async def test_get_active_game_for_pair_not_found_after_void(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "VOID")
    result = await hpdb.get_active_game_for_pair(db, GUILD, 1, 2)
    assert result is None


# ── get_pending_game_for_challenger ───────────────────────────────────────────

async def test_get_pending_game_for_challenger_found(db):
    gid = await _create(db)
    result = await hpdb.get_pending_game_for_challenger(db, GUILD, CH, 1)
    assert result is not None
    assert result.id == gid


async def test_get_pending_game_for_challenger_wrong_channel(db):
    await _create(db)
    result = await hpdb.get_pending_game_for_challenger(db, GUILD, 999, 1)
    assert result is None


# ── fetch helpers ──────────────────────────────────────────────────────────────

async def test_fetch_active_games(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "ACTIVE")
    games = await hpdb.fetch_active_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_resolved_games(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "RESOLVED")
    games = await hpdb.fetch_resolved_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_pending(db):
    gid = await _create(db)
    import sqlite3 as _sqlite3
    with _sqlite3.connect(str(db._db_path)) as conn:
        conn.execute(
            "UPDATE hot_potato_games SET created_at = ? WHERE id = ?",
            (time.time() - 120, gid),
        )
        conn.commit()
    games = await hpdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_active_timeout(db):
    gid = await _create(db)
    old = time.time() - 700
    await hpdb.set_game_state(db, gid, "ACTIVE", last_action_at=old)
    games = await hpdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_excludes_fresh(db):
    gid = await _create(db)
    await hpdb.set_game_state(db, gid, "ACTIVE", last_action_at=time.time())
    games = await hpdb.fetch_sweepable_games(db, time.time())
    assert not any(g.id == gid for g in games)


# ── config ────────────────────────────────────────────────────────────────────

async def test_get_config_defaults(db):
    cfg = await hpdb.get_config(db, GUILD)
    assert cfg["min_timer"] == pytest.approx(10.0)
    assert cfg["max_timer"] == pytest.approx(45.0)


async def test_upsert_config_updates_values(db):
    await hpdb.upsert_config(db, GUILD, min_timer=5.0)
    cfg = await hpdb.get_config(db, GUILD)
    assert cfg["min_timer"] == pytest.approx(5.0)
    assert cfg["max_timer"] == pytest.approx(45.0)  # untouched


async def test_upsert_config_idempotent(db):
    await hpdb.upsert_config(db, GUILD, min_timer=5.0)
    await hpdb.upsert_config(db, GUILD, min_timer=8.0)
    cfg = await hpdb.get_config(db, GUILD)
    assert cfg["min_timer"] == pytest.approx(8.0)


# ── style points ──────────────────────────────────────────────────────────────

async def test_get_style_points_empty(db):
    pts = await hpdb.get_style_points(db, GUILD, 99)
    assert pts == 0


async def test_add_style_points_accumulates(db):
    await hpdb.add_style_points(db, GUILD, 1, 15)
    await hpdb.add_style_points(db, GUILD, 1, 10)
    pts = await hpdb.get_style_points(db, GUILD, 1)
    assert pts == 25


async def test_add_style_points_separate_users(db):
    await hpdb.add_style_points(db, GUILD, 1, 20)
    await hpdb.add_style_points(db, GUILD, 2, 5)
    assert await hpdb.get_style_points(db, GUILD, 1) == 20
    assert await hpdb.get_style_points(db, GUILD, 2) == 5


# ── stats ─────────────────────────────────────────────────────────────────────

async def test_get_stats_empty(db):
    stats = await hpdb.get_stats(db, GUILD, 99)
    assert stats == {"wins": 0, "losses": 0, "total_games": 0, "style_points": 0}


async def test_get_stats_counts_wins_losses(db):
    gid1 = await _create(db)
    await hpdb.set_game_state(db, gid1, "RESOLVED", winner_id=1, loser_id=2)
    gid2 = await _create(db, challenger_id=2, target_id=1)
    await hpdb.set_game_state(db, gid2, "RESOLVED", winner_id=2, loser_id=1)

    stats = await hpdb.get_stats(db, GUILD, 1)
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["total_games"] == 2


async def test_get_stats_includes_style_points(db):
    await hpdb.add_style_points(db, GUILD, 1, 50)
    stats = await hpdb.get_stats(db, GUILD, 1)
    assert stats["style_points"] == 50
