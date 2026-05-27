"""Integration tests for pressure_cooker/db.py using GamesDb + real SQLite."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.services.games_db import GamesDb
from bot_modules.cogs.pressure_cooker import db as pdb


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
    return await pdb.create_game(db, **defaults)


# ── create / get ──────────────────────────────────────────────────────────────

async def test_create_and_get_game(db):
    gid = await _create(db)
    game = await pdb.get_game(db, gid)
    assert game is not None
    assert game.id == gid
    assert game.state == "PENDING"
    assert game.challenger_id == 1
    assert game.target_id == 2
    assert game.gauge == 0
    assert game.pumps == []


async def test_get_game_missing_returns_none(db):
    result = await pdb.get_game(db, 99999)
    assert result is None


async def test_create_game_stores_stakes_text(db):
    gid = await _create(db, stakes_text="loser buys pizza")
    game = await pdb.get_game(db, gid)
    assert game.stakes_text == "loser buys pizza"


# ── set_game_state ────────────────────────────────────────────────────────────

async def test_set_game_state_transitions(db):
    gid = await _create(db)
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=1)
    game = await pdb.get_game(db, gid)
    assert game.state == "ACTIVE"
    assert game.active_player == 1


async def test_set_game_state_extra_fields(db):
    gid = await _create(db)
    now = time.time()
    await pdb.set_game_state(db, gid, "RESOLVED", winner_id=2, loser_id=1, resolved_at=now)
    game = await pdb.get_game(db, gid)
    assert game.state == "RESOLVED"
    assert game.winner_id == 2
    assert game.loser_id == 1
    assert game.resolved_at == pytest.approx(now, abs=1)


# ── save_pump ─────────────────────────────────────────────────────────────────

async def test_save_pump_persists_fields(db):
    from bot_modules.cogs.pressure_cooker.game import apply_pump

    gid = await _create(db)
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=1, message_id=999)
    game = await pdb.get_game(db, gid)

    apply_pump(game, player_id=1, roll=7)
    await pdb.save_pump(db, game)

    reloaded = await pdb.get_game(db, gid)
    assert reloaded.gauge == 7
    assert reloaded.active_player == 2
    assert len(reloaded.pumps) == 1
    assert reloaded.pumps[0].roll == 7
    assert reloaded.last_pump_at is not None


async def test_save_pump_bust_state(db):
    from bot_modules.cogs.pressure_cooker.game import apply_pump

    gid = await _create(db)
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=1)
    game = await pdb.get_game(db, gid)
    game.gauge = 90
    apply_pump(game, player_id=1, roll=15)
    assert game.state == "RESOLVED"
    await pdb.save_pump(db, game)

    reloaded = await pdb.get_game(db, gid)
    assert reloaded.state == "RESOLVED"
    assert reloaded.loser_id == 1
    assert reloaded.winner_id == 2


# ── fetch helpers ─────────────────────────────────────────────────────────────

async def test_fetch_active_games_only_active(db):
    gid_active = await _create(db, challenger_id=10, target_id=11)
    gid_pending = await _create(db, challenger_id=12, target_id=13)
    await pdb.set_game_state(db, gid_active, "ACTIVE", active_player=10)

    games = await pdb.fetch_active_games(db)
    ids = {g.id for g in games}
    assert gid_active in ids
    assert gid_pending not in ids


async def test_fetch_resolved_games(db):
    gid = await _create(db)
    await pdb.set_game_state(db, gid, "RESOLVED", winner_id=2, loser_id=1, resolved_at=time.time())
    games = await pdb.fetch_resolved_games(db)
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_pending(db):
    gid = await _create(db)
    # Backdating created_at via direct SQL to simulate old pending game
    await db.execute(
        "UPDATE pressure_games SET created_at = ? WHERE id = ?",
        (time.time() - 120, gid),
    )
    games = await pdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_active_idle(db):
    gid = await _create(db)
    old = time.time() - 400
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=1)
    await db.execute(
        "UPDATE pressure_games SET last_pump_at = ? WHERE id = ?",
        (old, gid),
    )
    games = await pdb.fetch_sweepable_games(db, time.time())
    assert any(g.id == gid for g in games)


async def test_fetch_sweepable_fresh_pending_excluded(db):
    gid = await _create(db)
    games = await pdb.fetch_sweepable_games(db, time.time())
    assert not any(g.id == gid for g in games)


# ── get_active_game_for_pair ──────────────────────────────────────────────────

async def test_get_active_game_for_pair_finds_game(db):
    gid = await _create(db, challenger_id=1, target_id=2)
    result = await pdb.get_active_game_for_pair(db, GUILD, 1, 2)
    assert result is not None
    assert result.id == gid


async def test_get_active_game_for_pair_reversed_order(db):
    gid = await _create(db, challenger_id=1, target_id=2)
    result = await pdb.get_active_game_for_pair(db, GUILD, 2, 1)
    assert result is not None
    assert result.id == gid


async def test_get_active_game_for_pair_terminal_excluded(db):
    gid = await _create(db, challenger_id=1, target_id=2)
    await pdb.set_game_state(db, gid, "DECLINED")
    result = await pdb.get_active_game_for_pair(db, GUILD, 1, 2)
    assert result is None


async def test_get_active_game_for_pair_different_guild(db):
    await _create(db, guild_id=9001, challenger_id=1, target_id=2)
    result = await pdb.get_active_game_for_pair(db, 9002, 1, 2)
    assert result is None


# ── cooldowns ─────────────────────────────────────────────────────────────────

async def test_check_cooldown_no_record(db):
    result = await pdb.check_cooldown(db, GUILD, 1, 2, cooldown_hours=48)
    assert result is None


async def test_check_cooldown_within_window(db):
    await pdb.set_cooldown(db, GUILD, 1, 2)
    remaining = await pdb.check_cooldown(db, GUILD, 1, 2, cooldown_hours=48)
    assert remaining is not None
    assert remaining > 0
    assert remaining <= 48 * 3600


async def test_check_cooldown_canonical_order(db):
    """player_a is always min — both orderings should find the same row."""
    await pdb.set_cooldown(db, GUILD, 5, 3)
    r1 = await pdb.check_cooldown(db, GUILD, 3, 5, cooldown_hours=48)
    r2 = await pdb.check_cooldown(db, GUILD, 5, 3, cooldown_hours=48)
    assert r1 is not None
    assert r2 is not None


async def test_check_cooldown_expired_window(db):
    await pdb.set_cooldown(db, GUILD, 1, 2)
    # Check with 0-hour window — always expired
    result = await pdb.check_cooldown(db, GUILD, 1, 2, cooldown_hours=0)
    assert result is None


async def test_set_cooldown_upsert(db):
    await pdb.set_cooldown(db, GUILD, 1, 2)
    first = await pdb.check_cooldown(db, GUILD, 1, 2, cooldown_hours=48)
    await pdb.set_cooldown(db, GUILD, 1, 2)
    second = await pdb.check_cooldown(db, GUILD, 1, 2, cooldown_hours=48)
    # Both should return a remaining value; upsert should not error
    assert first is not None
    assert second is not None


# ── nicks ─────────────────────────────────────────────────────────────────────

async def test_apply_nick_and_fetch_expired(db):
    gid = await _create(db)
    nick_id = await pdb.apply_nick(
        db,
        game_id=gid,
        guild_id=GUILD,
        loser_id=1,
        winner_id=2,
        original_nick="OriginalName",
        imposed_nick="LoserFace",
        sentence_hours=0,  # expires immediately
    )
    assert nick_id > 0

    rows = await pdb.fetch_expired_nicks(db, time.time() + 1)
    assert any(r["id"] == nick_id for r in rows)


async def test_fetch_expired_nicks_excludes_future(db):
    gid = await _create(db)
    await pdb.apply_nick(
        db, gid, GUILD, 1, 2, "Original", "Imposed", sentence_hours=24
    )
    rows = await pdb.fetch_expired_nicks(db, time.time())
    # Future-expiry nick should not appear
    assert len(rows) == 0


async def test_mark_nick_reverted(db):
    gid = await _create(db)
    nick_id = await pdb.apply_nick(
        db, gid, GUILD, 1, 2, "OriginalName", "LoserFace", sentence_hours=0
    )
    await pdb.mark_nick_reverted(db, nick_id, "expired")
    rows = await pdb.fetch_expired_nicks(db, time.time() + 1)
    assert not any(r["id"] == nick_id for r in rows)


async def test_get_active_nick_for_user(db):
    gid = await _create(db)
    await pdb.apply_nick(db, gid, GUILD, 1, 2, "Original", "Imposed", sentence_hours=24)
    nick = await pdb.get_active_nick_for_user(db, GUILD, 1)
    assert nick is not None
    assert nick["imposed_nick"] == "Imposed"


async def test_get_active_nick_after_revert_returns_none(db):
    gid = await _create(db)
    nick_id = await pdb.apply_nick(
        db, gid, GUILD, 1, 2, "Original", "Imposed", sentence_hours=24
    )
    await pdb.mark_nick_reverted(db, nick_id, "manual")
    nick = await pdb.get_active_nick_for_user(db, GUILD, 1)
    assert nick is None


# ── stats ─────────────────────────────────────────────────────────────────────

async def test_get_stats_no_games(db):
    stats = await pdb.get_stats(db, GUILD, 99)
    assert stats == {"wins": 0, "losses": 0, "total_games": 0, "highest_gauge_win": None}


async def test_get_stats_counts_wins_and_losses(db):
    # Win for user 1
    g1 = await _create(db, challenger_id=1, target_id=2)
    await pdb.set_game_state(
        db, g1, "RESOLVED", winner_id=1, loser_id=2, resolved_at=time.time(), gauge=99
    )
    await db.execute("UPDATE pressure_games SET gauge = 99 WHERE id = ?", (g1,))

    # Loss for user 1
    g2 = await _create(db, challenger_id=1, target_id=2)
    await pdb.set_game_state(
        db, g2, "RESOLVED", winner_id=2, loser_id=1, resolved_at=time.time(), gauge=105
    )

    stats = await pdb.get_stats(db, GUILD, 1)
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["total_games"] == 2


# ── config ────────────────────────────────────────────────────────────────────

async def test_get_config_defaults_for_missing(db):
    cfg = await pdb.get_config(db, 9999)
    assert cfg["cooldown_hours"] == 48
    assert cfg["sentence_hours"] == 24
    assert cfg["allow_early_revert"] == 0


async def test_upsert_config_stores_fields(db):
    await pdb.upsert_config(db, GUILD, cooldown_hours=12, sentence_hours=6)
    cfg = await pdb.get_config(db, GUILD)
    assert cfg["cooldown_hours"] == 12
    assert cfg["sentence_hours"] == 6


async def test_upsert_config_partial_update(db):
    await pdb.upsert_config(db, GUILD, cooldown_hours=12, sentence_hours=6)
    await pdb.upsert_config(db, GUILD, cooldown_hours=24)
    cfg = await pdb.get_config(db, GUILD)
    assert cfg["cooldown_hours"] == 24
    assert cfg["sentence_hours"] == 6  # unchanged
