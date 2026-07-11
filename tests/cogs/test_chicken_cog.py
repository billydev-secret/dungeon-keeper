"""Cog-runtime tests for Chicken: crash resolution, bail flow, wipeout."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.chicken.cog import ChickenCog
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeGuild, fake_interaction

GUILD = 9001
CH = 100


class FakeBot:
    def __init__(self, db: GamesDb) -> None:
        self.games_db = db

    def add_view(self, *a, **k) -> None:
        pass

    def get_guild(self, gid):
        return None

    def get_channel(self, cid):
        return None


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest_asyncio.fixture
async def cog(db: GamesDb) -> ChickenCog:
    return ChickenCog(FakeBot(db))  # type: ignore[arg-type]


async def _climbing(db, *, alive, bail_log=None, roster=None, stakes=None):
    roster = roster or alive
    gid = await chdb.create_lobby(db, GUILD, CH, roster[0], stakes)
    now = time.time()
    await chdb.set_game_state(
        db, gid, "ACTIVE",
        phase="CLIMBING",
        roster=json.dumps(roster),
        alive=json.dumps(alive),
        bail_log=json.dumps(bail_log or []),
        climb_started_at=now - 5.0,
        climb_duration=25.0,
    )
    return await chdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_cog_exposes_group(db):
    cog = ChickenCog(FakeBot(db))  # type: ignore[arg-type]
    assert "chicken" in {c.name for c in cog.get_app_commands()}


def test_build_view_has_bail_button(db):
    view = ChickenCog(FakeBot(db)).build_game_view(9)  # type: ignore[arg-type]
    assert "chicken_bail:9" in [getattr(c, "custom_id", None) for c in view.children]


# ── _crash resolution ──────────────────────────────────────────────────────────

async def test_crash_with_bailer_nicks_one_crasher(cog, db):
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    await cog._crash(game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 3          # bravest bailer
    assert g.loser_id == 1           # deterministic crasher


async def test_crash_total_wipeout_no_nick(cog, db):
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    await cog._crash(game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED_NO_NICK"
    assert g.winner_id is None
    assert g.loser_id is None


async def test_crash_sets_group_cooldowns(cog, db):
    from bot_modules.duels import db as duels_db
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    await cog._crash(game.id)
    for uid in (1, 2, 3):
        assert await duels_db.check_group_cooldown(db, GUILD, "chicken", uid, 48) is not None


# ── economy payouts (Stage 1 faucet) ─────────────────────────────────────────

class _EconBot:
    """Bot with economy config reachable: resolves members, no channel sends."""

    def __init__(self, games_db: GamesDb, db_path: Path, member_ids) -> None:
        self.games_db = games_db
        self.ctx = SimpleNamespace(db_path=db_path)
        self.active_views: dict = {}
        members = {
            uid: SimpleNamespace(id=uid, bot=False, premium_since=None,
                                 display_name=f"U{uid}")
            for uid in member_ids
        }
        self._guild = FakeGuild(id=GUILD, members=members)

    def add_view(self, *a, **k) -> None:
        pass

    def get_guild(self, gid):
        return self._guild if gid == GUILD else None

    def get_channel(self, cid):
        return None  # skip result rendering; payout still fires


async def test_crash_pays_winner_and_losers(db, sync_db_path):
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(_EconBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)  # winner = 3 (bravest bailer)
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 3) == 25  # participation + win
        assert get_balance(conn, GUILD, 1) == 5   # participation only
        assert get_balance(conn, GUILD, 2) == 5


async def test_crash_no_payout_when_disabled(db, sync_db_path):
    cog = ChickenCog(_EconBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)  # total wipeout, economy disabled
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 0


# ── _on_bail flow ──────────────────────────────────────────────────────────────

async def test_bail_removes_player_and_continues(cog, db):
    game = await _climbing(db, alive=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2
    await cog._on_bail(interaction, game.id)
    g = await chdb.get_game(db, game.id)
    assert g.alive == [1, 3]
    assert len(g.bail_log) == 1
    assert g.bail_log[0]["player_id"] == 2
    assert g.state == "ACTIVE"
    interaction.edit_original_response.assert_awaited()


async def test_bail_last_holder_everyone_blinked(cog, db):
    game = await _climbing(db, alive=[1], bail_log=[
        {"player_id": 2, "bail_ts": time.time(), "meter_pct": 40.0}
    ], roster=[1, 2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await cog._on_bail(interaction, game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED_NO_NICK"
    assert g.winner_id == 1          # last to bail wins, no nick
    assert g.loser_id is None


async def test_bail_rejects_already_bailed(cog, db):
    game = await _climbing(db, alive=[1, 3], bail_log=[
        {"player_id": 2, "bail_ts": time.time(), "meter_pct": 30.0}
    ], roster=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2  # already bailed → not in alive
    await cog._on_bail(interaction, game.id)
    g = await chdb.get_game(db, game.id)
    assert g.alive == [1, 3]
    interaction.followup.send.assert_awaited()


# ── lobby start (custom-stakes mode skips nick preflight) ──────────────────────

async def test_lobby_start_begins_climb(cog, db):
    gid = await chdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await chdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]), message_id=42)
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    try:
        await cog._handle_lobby_start(interaction, gid)
        g = await chdb.get_game(db, gid)
        assert g.state == "ACTIVE"
        assert g.phase == "CLIMBING"
        assert g.alive == [1, 2]
        assert g.climb_started_at is not None
    finally:
        cog._cancel_timers(gid)
