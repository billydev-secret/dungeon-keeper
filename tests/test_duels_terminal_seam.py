"""The terminal-state seam: every game end is observable in one place.

Stage 4a of the economy sinks round made BaseGame._db_set_state a concrete
template method — cogs write state through _db_write_state, and every
terminal transition fires _on_terminal_state. These tests pin that guarantee,
which is what stage 4b's wager escrow will hang settlements and refunds on.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.chicken.cog import ChickenCog
from bot_modules.cogs.quickdraw import db as qdb
from bot_modules.cogs.quickdraw.cog import QuickdrawDuel
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


class RecordingChicken(ChickenCog):
    """Chicken with a spy on the terminal-state hook."""

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.seen: list[tuple[int, str]] = []

    async def _on_terminal_state(self, game_id: int, state: str) -> None:
        self.seen.append((game_id, state))
        await super()._on_terminal_state(game_id, state)


class RecordingQuickdraw(QuickdrawDuel):
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.seen: list[tuple[int, str]] = []

    async def _on_terminal_state(self, game_id: int, state: str) -> None:
        self.seen.append((game_id, state))
        await super()._on_terminal_state(game_id, state)


def _chicken(db: GamesDb, db_path: Path) -> RecordingChicken:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    return RecordingChicken(FakeEconGamesBot(db, db_path, [1, 2, 3]))  # type: ignore[arg-type]


async def _climbing(db: GamesDb, roster: list[int]):
    gid = await chdb.create_lobby(db, GUILD, CH, roster[0], None)
    now = time.time()
    await chdb.set_game_state(
        db, gid, "ACTIVE",
        phase="CLIMBING",
        roster=json.dumps(roster),
        alive=json.dumps(roster),
        bail_log="[]",
        climb_started_at=now - 5.0,
        climb_duration=25.0,
    )
    return await chdb.get_game(db, gid)


# ── Every game end reaches the hook ────────────────────────────────────────────

async def test_resolution_fires_hook_once(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    game = await _climbing(db, [1, 2, 3])
    await cog._crash(game.id)  # total wipeout → RESOLVED_NO_NICK
    assert cog.seen == [(game.id, "RESOLVED_NO_NICK")]


async def test_abandonment_fires_hook_without_payout(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    game = await _climbing(db, [1, 2])
    await cog._expire_active(game)
    assert cog.seen == [(game.id, "ABANDONED")]
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 0


async def test_expired_pending_fires_hook(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, None)
    game = await chdb.get_game(db, gid)
    await cog._expire_pending(game)
    assert cog.seen == [(gid, "EXPIRED_PENDING")]


async def test_lobby_cancel_fires_hook(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, None)
    await chdb.set_game_state(db, gid, "LOBBY")
    interaction = fake_interaction()
    interaction.user.id = 1
    await cog._handle_lobby_cancel(interaction, gid)
    assert cog.seen == [(gid, "EXPIRED_LOBBY")]
    assert (await chdb.get_game(db, gid)).state == "EXPIRED_LOBBY"


async def test_non_terminal_writes_do_not_fire_hook(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    game = await _climbing(db, [1, 2, 3])
    await cog._db_set_state(game.id, "ACTIVE", last_action_at=time.time())
    assert cog.seen == []


async def test_void_fires_hook_without_payout(db, sync_db_path):
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = RecordingQuickdraw(FakeEconGamesBot(db, sync_db_path, [1, 2]))  # type: ignore[arg-type]
    gid = await qdb.create_game(db, GUILD, CH, 1, 2, None)
    await qdb.set_game_state(db, gid, "ACTIVE", qd_state="DRAW", fired_at=time.time())
    await cog._fire_void(gid)
    assert cog.seen == [(gid, "VOID")]
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 0
        assert get_balance(conn, GUILD, 2) == 0


# ── Payout failures never propagate into game flow ─────────────────────────────

async def test_hook_failure_never_breaks_resolution(db, sync_db_path, monkeypatch):
    cog = _chicken(db, sync_db_path)

    async def _boom(game_id):
        raise RuntimeError("economy exploded")

    monkeypatch.setattr(cog, "_db_get_game", _boom)
    game = await _climbing(db, [1, 2, 3])
    await cog._db_set_state(game.id, "RESOLVED_NO_NICK", winner_id=None)
    assert (await chdb.get_game(db, game.id)).state == "RESOLVED_NO_NICK"
