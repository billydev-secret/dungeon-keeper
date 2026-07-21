"""Cog-runtime tests for Chicken: crash resolution, bail flow, wipeout."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.chicken.cog import ChickenCog
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED, COLOR_YELLOW
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeGuild, fake_interaction

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


async def test_crash_pays_winner_and_losers(db, sync_db_path):
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)  # winner = 3 (bravest bailer)
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 3) == 25  # participation + win
        assert get_balance(conn, GUILD, 1) == 5   # participation only
        assert get_balance(conn, GUILD, 2) == 5


async def test_crash_no_payout_when_disabled(db, sync_db_path):
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)  # total wipeout, economy disabled
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 0


async def test_wipeout_pays_participation_only(db, sync_db_path):
    """Total wipeout (winner=None) still reaches the payout: everyone gets
    participation, nobody gets the win bonus."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED_NO_NICK"
    assert g.winner_id is None
    with open_db(sync_db_path) as conn:
        for uid in (1, 2, 3):
            assert get_balance(conn, GUILD, uid) == 5


async def test_expire_active_abandons_without_payout(db, sync_db_path):
    """An abandoned game terminalizes with zero economy effect — the most
    important branch for the wager refund path (stage 4b) to hook later."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._expire_active(game)
    g = await chdb.get_game(db, game.id)
    assert g.state == "ABANDONED"
    with open_db(sync_db_path) as conn:
        for uid in (1, 2, 3):
            assert get_balance(conn, GUILD, uid) == 0


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


# ── embed colors (accent + win=green / loss=red) ───────────────────────────────

async def test_active_card_uses_guild_accent(cog, db, monkeypatch):
    """The live climb card follows the resolved guild accent."""
    accent = discord.Color(0x5865F2)
    from bot_modules.cogs.chicken import cog as chicken_cog
    monkeypatch.setattr(
        chicken_cog, "resolve_accent_color", AsyncMock(return_value=accent)
    )
    # The bare FakeBot has no ``ctx``; give it one so accent resolution runs.
    cog.bot.ctx = SimpleNamespace(db_path=Path("unused.db"))
    game = await _climbing(db, alive=[1, 2, 3])

    resolved = await cog._resolve_accent(FakeGuild())
    assert resolved == accent
    embed = cog.render_game_state(game, FakeGuild(), resolved)
    assert embed.color == accent


async def test_active_card_falls_back_to_yellow_without_context(cog, db):
    """No ctx / branding hiccup → the old warning yellow, never a crash."""
    game = await _climbing(db, alive=[1, 2, 3])
    # FakeBot exposes no ``ctx`` → helper short-circuits to COLOR_YELLOW.
    assert await cog._resolve_accent(FakeGuild()) == COLOR_YELLOW
    embed = cog.render_game_state(game, FakeGuild())
    assert embed.color.value == COLOR_YELLOW


async def test_winner_embed_is_green(cog, db):
    """Everyone-blinked winner card is a WIN → green."""
    game = await _climbing(
        db,
        alive=[],
        bail_log=[{"player_id": 1, "bail_ts": time.time(), "meter_pct": 80.0}],
        roster=[1, 2],
    )
    game.winner_id = 1
    game.loser_id = None
    embed = cog.render_result_state(game, FakeGuild())
    assert embed.color.value == COLOR_GREEN


async def test_crash_embed_stays_red(cog, db):
    """A crash with a nick loser is a genuine LOSS → red."""
    game = await _climbing(
        db,
        alive=[1],
        bail_log=[{"player_id": 2, "bail_ts": time.time(), "meter_pct": 60.0}],
        roster=[1, 2],
    )
    game.loser_id = 1
    game.winner_id = 2
    embed = cog.render_result_state(game, FakeGuild())
    assert embed.color.value == COLOR_RED


async def test_total_wipeout_stays_red(cog, db):
    """Total wipeout — nobody wins → red."""
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    game.winner_id = None
    game.loser_id = None
    embed = cog.render_result_state(game, FakeGuild())
    assert embed.color.value == COLOR_RED
