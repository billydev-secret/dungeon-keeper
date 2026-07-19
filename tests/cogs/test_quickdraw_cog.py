"""Cog-runtime tests for Quickdraw: VOID, false start, and the payout branches.

These pin the resolution paths the economy seam funnels through: a voided
round pays nothing, every genuinely-played round pays participation + win.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

from bot_modules.cogs.quickdraw import db as qdb
from bot_modules.cogs.quickdraw.cog import QuickdrawDuel
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, fake_interaction

GUILD = 9001
CH = 100
P1, P2 = 1, 2


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _econ_cog(db: GamesDb, db_path: Path, *, with_channel: bool = False) -> QuickdrawDuel:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, db_path, [P1, P2], with_channel=with_channel)
    return QuickdrawDuel(bot)  # type: ignore[arg-type]


async def _active_game(db: GamesDb, *, qd_state: str, **extra):
    gid = await qdb.create_game(db, GUILD, CH, P1, P2, None)
    await qdb.set_game_state(db, gid, "ACTIVE", qd_state=qd_state, **extra)
    return await qdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_build_view_has_fire_button(db, sync_db_path):
    view = _econ_cog(db, sync_db_path).build_game_view(9)
    assert "fire:9" in [getattr(c, "custom_id", None) for c in view.children]


# ── VOID: nobody fired ─────────────────────────────────────────────────────────

async def test_void_when_nobody_fires_pays_nothing(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _active_game(db, qd_state="DRAW", fired_at=time.time())
    await cog._fire_void(game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "VOID"
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 0
        assert get_balance(conn, GUILD, P2) == 0


# ── False start: presser loses immediately ─────────────────────────────────────

async def test_false_start_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _active_game(db, qd_state="WAITING")
    interaction = fake_interaction()
    interaction.user.id = P2
    interaction.guild = cog.bot.guild
    interaction.followup.send.return_value = SimpleNamespace(id=901)
    await cog._handle_game_button(interaction, game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == P1
    assert g.loser_id == P2
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25   # participation + win
        assert get_balance(conn, GUILD, P2) == 5    # participation only


# ── Winner fired, opponent answers second ──────────────────────────────────────

async def test_opponent_second_fire_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    now = time.time()
    game = await _active_game(
        db, qd_state="WINNER_FIRED",
        winner_id=P1, loser_id=P2, fired_at=now - 1.0, resolved_at=now - 0.5,
    )
    interaction = fake_interaction()
    interaction.user.id = P2
    interaction.guild = cog.bot.guild
    interaction.followup.send.return_value = SimpleNamespace(id=902)
    await cog._handle_game_button(interaction, game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.loser_fired_at is not None
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25
        assert get_balance(conn, GUILD, P2) == 5


# ── Winner fired, opponent never answers (draw window expires) ─────────────────

async def test_winner_fired_void_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path, with_channel=True)
    now = time.time()
    game = await _active_game(
        db, qd_state="WINNER_FIRED",
        winner_id=P1, loser_id=P2, fired_at=now - 6.0, resolved_at=now - 5.5,
    )
    await cog._fire_void(game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.qd_state == "COMPLETE"
    assert g.result_message_id is not None
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25
        assert get_balance(conn, GUILD, P2) == 5
