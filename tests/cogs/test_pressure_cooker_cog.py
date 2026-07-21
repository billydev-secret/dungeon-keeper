"""Cog-runtime tests for Pressure Cooker: the busted-pump resolution + payout.

The bust path is one of the economy seam's funnel points: the presser who
pushes the gauge over the ceiling loses, the opponent wins, both get paid.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

from bot_modules.cogs.pressure_cooker import db as pdb
from bot_modules.cogs.pressure_cooker.cog import PressureCookerDuel
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


def _econ_cog(db: GamesDb, db_path: Path) -> PressureCookerDuel:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, db_path, [P1, P2])
    return PressureCookerDuel(bot)  # type: ignore[arg-type]


async def test_bust_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    gid = await pdb.create_game(db, GUILD, CH, P1, P2, None)
    # Gauge at 99: any roll busts, so the press below always loses for P1.
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=P1, gauge=99)
    interaction = fake_interaction()
    interaction.user.id = P1
    interaction.guild = cog.bot.guild
    interaction.followup.send.return_value = SimpleNamespace(id=903)
    await cog._handle_game_button(interaction, gid)
    g = await pdb.get_game(db, gid)
    assert g.state == "RESOLVED"
    assert g.winner_id == P2
    assert g.loser_id == P1
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P2) == 25   # participation + win
        assert get_balance(conn, GUILD, P1) == 5    # participation only


async def test_out_of_turn_press_pays_nothing(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    gid = await pdb.create_game(db, GUILD, CH, P1, P2, None)
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=P1, gauge=99)
    interaction = fake_interaction()
    interaction.user.id = P2  # not their turn
    interaction.guild = cog.bot.guild
    await cog._handle_game_button(interaction, gid)
    g = await pdb.get_game(db, gid)
    assert g.state == "ACTIVE"
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 0
        assert get_balance(conn, GUILD, P2) == 0
