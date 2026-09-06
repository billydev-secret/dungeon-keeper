"""``/games end`` on a game that owns a recap ending.

Would You Rather, Never Have I Ever and Most Likely To end themselves with a
recap through the paying path (``end_with_recap``); ``/games end`` hands them
the close so the room sees that card rather than the red Force-Closed one
(vote-games-52 / discovery-3). Anything else — an unknown type, a cog that is
not loaded, an MLT lobby with nothing to recap — keeps the force-close path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_modules.cogs.games_config_cog as mod
from bot_modules.cogs.games_config_cog import GamesConfigCog
from bot_modules.games.utils.game_manager import create_game, get_active_game_by_id
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeMessageableChannel

HOST = 1


def _cog(db_path, cogs: dict):
    bot = SimpleNamespace(
        games_db=GamesDb(db_path), active_views={}, ctx=SimpleNamespace(db_path=db_path),
        get_cog=lambda name: cogs.get(name),
    )
    return GamesConfigCog(bot)  # type: ignore[arg-type]


def _interaction(channel):
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host", roles=[]),
        guild=None, guild_id=None, channel_id=channel.id, channel=channel,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def _confirm(interaction):
    """Press "Yes, End Game" on the confirmation the command sent."""
    view = interaction.response.send_message.await_args.kwargs["view"]
    confirm = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    await view.confirm.callback(confirm)


@pytest.mark.parametrize("game_type, cog_name", [("wyr", "WYRCog"), ("nhie", "NHIECog"), ("mlt", "MLTCog")])
async def test_games_end_hands_the_close_to_the_games_recap_ending(sync_db_path, game_type, cog_name):
    finisher = AsyncMock(return_value=True)
    cog = _cog(sync_db_path, {cog_name: SimpleNamespace(end_with_recap=finisher)})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, game_type, state="playing", payload={"rounds": {"1": {}}})
    interaction = _interaction(channel)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]
    await _confirm(interaction)

    finisher.assert_awaited_once_with(channel, gid)
    assert channel.sent == [], "no Force-Closed card when the game posted its own recap"


async def test_games_end_falls_back_to_force_close_when_there_is_nothing_to_recap(sync_db_path, monkeypatch):
    """An MLT lobby that never started answers False from its finisher."""
    finisher = AsyncMock(return_value=False)
    cog = _cog(sync_db_path, {"MLTCog": SimpleNamespace(end_with_recap=finisher)})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, "mlt", state="joining", payload={"rounds": {}, "players": []})
    force_end = AsyncMock()
    monkeypatch.setattr(mod, "force_end_active_game", force_end)
    interaction = _interaction(channel)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]
    await _confirm(interaction)

    finisher.assert_awaited_once_with(channel, gid)
    force_end.assert_awaited_once()
    assert channel.sent and "Force-Closed" in channel.sent[-1]["embed"].title


async def test_games_end_force_closes_a_game_whose_cog_is_not_loaded(sync_db_path):
    cog = _cog(sync_db_path, {})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, "wyr", state="playing", payload={"rounds": {}})
    interaction = _interaction(channel)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]
    await _confirm(interaction)

    assert await get_active_game_by_id(cog.db, gid) is None
    assert channel.sent and "Force-Closed" in channel.sent[-1]["embed"].title
