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


# ── Wager escrow rides the seam (stage 4b) ────────────────────────────────────


def _fund(db_path: Path, user_id: int, amount: int) -> None:
    from bot_modules.services.economy_service import apply_credit

    with open_db(db_path) as conn:
        apply_credit(conn, GUILD, user_id, amount, "grant")


def _mute_faucet(db_path: Path) -> None:
    """Zero the participation/win rewards so a balance shows the WAGER only.

    The faucet still fires on a resolution (it is a separate concern from the
    pot); these tests are about escrow, so they isolate it.
    """
    with open_db(db_path) as conn:
        save_econ_settings(
            conn, GUILD,
            {"reward_game_participation": 0, "reward_game_win": 0},
        )


def _stake(db_path: Path, game_type: str, game_id: int, user_id: int, amount: int):
    from bot_modules.services import economy_wager_service as wager_svc

    with open_db(db_path) as conn:
        wager_svc.hold_stake(conn, GUILD, game_type, game_id, user_id, amount)


def _balances(db_path: Path, *users: int) -> list[int]:
    with open_db(db_path) as conn:
        return [get_balance(conn, GUILD, u) for u in users]


async def test_resolution_pays_pot_to_winner(db, sync_db_path):
    """A won game settles the escrow to the winner via the same hook."""
    cog = _chicken(db, sync_db_path)
    _mute_faucet(sync_db_path)
    game = await _climbing(db, [1, 2, 3])
    for uid in (1, 2, 3):
        _fund(sync_db_path, uid, 100)
        _stake(sync_db_path, cog.GAME_KEY, game.id, uid, 50)
    assert _balances(sync_db_path, 1, 2, 3) == [50, 50, 50]

    # Two bail, so player 3 is the last one holding and wins.
    await cog._on_bail(_bail_interaction(1), game.id)
    await cog._on_bail(_bail_interaction(2), game.id)
    await cog._crash(game.id)

    a, b, c = _balances(sync_db_path, 1, 2, 3)
    # The pot (150) went to exactly one player; nothing was minted or lost.
    assert a + b + c == 300
    assert sorted([a, b, c]) == [50, 50, 200]


async def test_abandoned_game_refunds_every_stake(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    game = await _climbing(db, [1, 2])
    for uid in (1, 2):
        _fund(sync_db_path, uid, 100)
        _stake(sync_db_path, cog.GAME_KEY, game.id, uid, 50)

    await cog._expire_active(game)  # ABANDONED — the plan's "silently vanishes"

    assert _balances(sync_db_path, 1, 2) == [100, 100]


async def test_wipeout_refunds_rather_than_paying_nobody(db, sync_db_path):
    """Chicken total wipeout resolves with winner_id None."""
    cog = _chicken(db, sync_db_path)
    _mute_faucet(sync_db_path)
    game = await _climbing(db, [1, 2, 3])
    for uid in (1, 2, 3):
        _fund(sync_db_path, uid, 100)
        _stake(sync_db_path, cog.GAME_KEY, game.id, uid, 50)

    await cog._crash(game.id)  # nobody bailed → RESOLVED_NO_NICK, no winner

    assert _balances(sync_db_path, 1, 2, 3) == [100, 100, 100]


async def test_quickdraw_void_refunds_both(db, sync_db_path):
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = RecordingQuickdraw(FakeEconGamesBot(db, sync_db_path, [1, 2]))  # type: ignore[arg-type]
    gid = await qdb.create_game(db, GUILD, CH, 1, 2, None)
    await qdb.set_game_state(db, gid, "ACTIVE", qd_state="DRAW", fired_at=time.time())
    for uid in (1, 2):
        _fund(sync_db_path, uid, 100)
        _stake(sync_db_path, cog.GAME_KEY, gid, uid, 25)

    await cog._fire_void(gid)  # nobody fired

    assert _balances(sync_db_path, 1, 2) == [100, 100]


async def test_lobby_cancel_refunds_the_pot(db, sync_db_path):
    cog = _chicken(db, sync_db_path)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, None)
    await chdb.set_game_state(db, gid, "LOBBY")
    for uid in (1, 2):
        _fund(sync_db_path, uid, 100)
        _stake(sync_db_path, cog.GAME_KEY, gid, uid, 30)

    interaction = fake_interaction()
    interaction.user.id = 1
    await cog._handle_lobby_cancel(interaction, gid)

    assert _balances(sync_db_path, 1, 2) == [100, 100]


async def test_replayed_terminal_hook_pays_once(db, sync_db_path):
    """The sweep and the resume path can both re-fire a terminal state."""
    cog = _chicken(db, sync_db_path)
    game = await _climbing(db, [1, 2])
    for uid in (1, 2):
        _fund(sync_db_path, uid, 100)
        _stake(sync_db_path, cog.GAME_KEY, game.id, uid, 50)

    await cog._expire_active(game)
    before = _balances(sync_db_path, 1, 2)
    await cog._on_terminal_state(game.id, "ABANDONED")  # replay
    await cog._on_terminal_state(game.id, "ABANDONED")  # and again

    assert _balances(sync_db_path, 1, 2) == before == [100, 100]


def _bail_interaction(user_id: int):
    interaction = fake_interaction()
    interaction.user.id = user_id
    return interaction
