"""Mt. Rushmore's recap relaunch goes through the slash entry's gate.

The guard's branches are pinned in ``test_games_price_cog.py``; this proves
Rushmore's Run Again is wired through it too.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_modules.cogs.games_rushmore_cog as cog_module
from bot_modules.cogs.games_rushmore_cog import RushmoreCog, RushmoreRecapView
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 778
HOST = 1


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host"),
        guild_id=GUILD,
        channel_id=CHAN,
        channel=SimpleNamespace(id=CHAN, name="games", guild=None),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
async def test_run_again_honours_the_enabled_dial(sync_db_path, enabled, monkeypatch):
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = RushmoreCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    await cog.db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, ?)",
        (GUILD, "rushmore", int(enabled)),
    )
    view = RushmoreRecapView("old-gid", HOST, cog, {"mode": "blitz"})
    interaction = _interaction()

    await view.run_again.callback(interaction)  # type: ignore[arg-type]

    if enabled:
        launch.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
    else:
        launch.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "disabled" in interaction.response.send_message.await_args.args[0]
        interaction.message.edit.assert_not_awaited()
