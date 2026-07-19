"""Integration tests for quickdraw/db.py using GamesDb + real SQLite."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.services.games_db import GamesDb
from bot_modules.cogs.quickdraw import db as qdb


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
    return await qdb.create_game(db, **defaults)


# ── create / get ──────────────────────────────────────────────────────────────

async def test_create_and_get_game(db):
    gid = await _create(db)
    game = await qdb.get_game(db, gid)
    assert game is not None
    assert game.id == gid
    assert game.state == "PENDING"
    assert game.qd_state == "WAITING"
    assert game.challenger_id == 1
    assert game.target_id == 2
    assert game.fired_at is None


async def test_get_game_missing_returns_none(db):
    result = await qdb.get_game(db, 99999)
    assert result is None


async def test_create_game_stores_stakes_text(db):
    gid = await _create(db, stakes_text="loser picks up tab")
    game = await qdb.get_game(db, gid)
    assert game.stakes_text == "loser picks up tab"


# ── set_game_state ─────────────────────────────────────────────────────────────

async def test_set_game_state_transitions(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "ACTIVE", qd_state="WAITING")
    game = await qdb.get_game(db, gid)
    assert game.state == "ACTIVE"
    assert game.qd_state == "WAITING"


async def test_set_game_state_to_draw(db):
    gid = await _create(db)
    fired = time.time()
    await qdb.set_game_state(db, gid, "ACTIVE", qd_state="DRAW", fired_at=fired)
    game = await qdb.get_game(db, gid)
    assert game.qd_state == "DRAW"
    assert game.fired_at == pytest.approx(fired, abs=1)


async def test_set_game_state_to_complete_false_start(db):
    gid = await _create(db)
    now = time.time()
    await qdb.set_game_state(
        db, gid, "ACTIVE",
        qd_state="COMPLETE",
        winner_id=2,
        loser_id=1,
        resolved_at=now,
    )
    game = await qdb.get_game(db, gid)
    assert game.qd_state == "COMPLETE"
    assert game.winner_id == 2
    assert game.loser_id == 1
    assert game.fired_at is None  # false start — fired_at never set


async def test_set_game_state_to_void(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "VOID")
    game = await qdb.get_game(db, gid)
    assert game.state == "VOID"


# ── get_active_game_for_pair ──────────────────────────────────────────────────

async def test_get_active_game_for_pair_found(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "ACTIVE")
    result = await qdb.get_active_game_for_pair(db, GUILD, 1, 2)
    assert result is not None
    assert result.id == gid


async def test_get_active_game_for_pair_reversed_order(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "ACTIVE")
    result = await qdb.get_active_game_for_pair(db, GUILD, 2, 1)
    assert result is not None
    assert result.id == gid


async def test_get_active_game_for_pair_not_found_for_void(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "VOID")
    result = await qdb.get_active_game_for_pair(db, GUILD, 1, 2)
    assert result is None


# ── get_pending_game_for_challenger ───────────────────────────────────────────

async def test_get_pending_game_for_challenger(db):
    gid = await _create(db)
    result = await qdb.get_pending_game_for_challenger(db, GUILD, CH, 1)
    assert result is not None
    assert result.id == gid


async def test_get_pending_game_for_challenger_wrong_channel(db):
    await _create(db)
    result = await qdb.get_pending_game_for_challenger(db, GUILD, 999, 1)
    assert result is None


# ── fetch helpers ──────────────────────────────────────────────────────────────

async def test_fetch_active_games(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "ACTIVE")
    games = await qdb.fetch_active_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_resolved_games(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "RESOLVED")
    games = await qdb.fetch_resolved_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_pending(db):
    gid = await _create(db)
    # Backdate created_at to look old
    import sqlite3 as _sqlite3
    with _sqlite3.connect(str(db._db_path)) as conn:
        conn.execute(
            "UPDATE quickdraw_games SET created_at = ? WHERE id = ?",
            (time.time() - 120, gid),
        )
        conn.commit()
    games = await qdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_active_timeout(db):
    gid = await _create(db)
    old = time.time() - 700
    await qdb.set_game_state(db, gid, "ACTIVE", last_action_at=old)
    games = await qdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_excludes_fresh(db):
    gid = await _create(db)
    await qdb.set_game_state(db, gid, "ACTIVE", last_action_at=time.time())
    games = await qdb.fetch_sweepable_games(db, time.time())
    assert not any(g.id == gid for g in games)


# ── config ────────────────────────────────────────────────────────────────────

async def test_get_config_defaults(db):
    cfg = await qdb.get_config(db, GUILD)
    assert cfg["min_delay"] == pytest.approx(3.0)
    assert cfg["max_delay"] == pytest.approx(8.0)
    assert cfg["draw_window"] == pytest.approx(5.0)
    assert cfg["void_on_double_noshow"] == 1


async def test_upsert_config_updates_values(db):
    await qdb.upsert_config(db, GUILD, min_delay=2.0, draw_window=3.0)
    cfg = await qdb.get_config(db, GUILD)
    assert cfg["min_delay"] == pytest.approx(2.0)
    assert cfg["draw_window"] == pytest.approx(3.0)
    assert cfg["max_delay"] == pytest.approx(8.0)  # untouched


async def test_upsert_config_idempotent(db):
    await qdb.upsert_config(db, GUILD, min_delay=1.0)
    await qdb.upsert_config(db, GUILD, min_delay=2.0)
    cfg = await qdb.get_config(db, GUILD)
    assert cfg["min_delay"] == pytest.approx(2.0)
