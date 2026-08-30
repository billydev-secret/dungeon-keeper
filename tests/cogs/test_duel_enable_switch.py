"""Turning a duel game off actually stops it starting.

The six duel-style games (Pressure Cooker, Quickdraw, both Hot Potatoes,
Chicken, Musical Chairs) had no off switch at all. Their panels told admins
that emptying the allowed-channel list on Games › Global Config meant "this
game cannot be played anywhere", which was never true for them — that list is
only consulted by the question-bank games, and a duel game's own allowlist
means "everywhere" when it is empty.

They now share the enable switch every other game has: a games_game_config row
under the cog's GAME_KEY, written by the "Available on This Server" toggle on
each game's panel. No row still means enabled, so an untouched guild is
unaffected. These tests drive the two real creation entrypoints —
``_base_challenge`` (duel) and ``_base_lobby`` (lobby game).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import apply_credit, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeMember, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _set_enabled(sync_db_path: Path, game_type: str, enabled: bool) -> None:
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(guild_id, game_type) DO UPDATE SET enabled = excluded.enabled",
            (GUILD, game_type, int(enabled)),
        )


def _seed_economy(sync_db_path: Path, *user_ids: int, amount: int = 500) -> None:
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        for uid in user_ids:
            apply_credit(conn, GUILD, uid, amount, "test_seed")


def _interaction(bot, user_id: int):
    bot.guild.me = None  # accent fallback; the nick preflight would crash on it
    i = fake_interaction(user=FakeMember(id=user_id), guild=bot.guild, channel_id=CH)
    i.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    return i


def _refusals(interaction) -> list[str]:
    return [c.args[0] for c in interaction.response.send_message.call_args_list if c.args]


async def test_disabled_duel_refuses_the_challenge(db, sync_db_path):
    _set_enabled(sync_db_path, "hot_potato", False)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _interaction(bot, 1)

    await cog._base_challenge(interaction, FakeMember(id=2), None)

    assert await hpdb.get_game(db, 1) is None
    assert any("switched off" in msg for msg in _refusals(interaction))


async def test_disabled_lobby_game_refuses_the_start(db, sync_db_path):
    _set_enabled(sync_db_path, "hot_potato_group", False)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    interaction = _interaction(bot, 1)

    await cog._base_lobby(interaction, None)

    assert await hpgdb.get_game(db, 1) is None
    assert any("switched off" in msg for msg in _refusals(interaction))


@pytest.mark.parametrize("stored", [True, None], ids=["explicitly-on", "never-set"])
async def test_an_enabled_or_untouched_game_still_starts(db, sync_db_path, stored):
    _seed_economy(sync_db_path, 1)
    if stored is not None:
        _set_enabled(sync_db_path, "hot_potato", stored)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    # A coin wager keeps this off the rename path, whose preflight needs a
    # guild.me the fakes deliberately don't have.
    await cog._base_challenge(_interaction(bot, 1), FakeMember(id=2), None, wager=50)

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.state == "PENDING"


async def test_one_game_being_off_does_not_switch_off_another(db, sync_db_path):
    """The row is keyed by game type — Quickdraw off is not Hot Potato off."""
    _seed_economy(sync_db_path, 1)
    _set_enabled(sync_db_path, "quickdraw", False)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(_interaction(bot, 1), FakeMember(id=2), None, wager=50)

    assert await hpdb.get_game(db, 1) is not None
