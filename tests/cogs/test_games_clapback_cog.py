"""Clapback's recap relaunch goes through the slash entry's gate.

The guard's branches are pinned in ``test_games_price_cog.py``; this proves
both of Clapback's Play Again buttons are wired through it. They call
``_start_new_game`` rather than ``launch`` (the recap carries a fully built
config), so the gate has to sit on the button itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_modules.cogs.games_clapback_cog as cog_module
from bot_modules.cogs.games_clapback_cog import ClapbackCog, ClapbackRecapView
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 779
HOST = 1


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host"),
        guild=None,
        guild_id=GUILD,
        channel_id=CHAN,
        channel=SimpleNamespace(id=CHAN, name="games", guild=None, send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()
        ),
    )


@pytest.mark.parametrize("button", ["play_again", "play_again_shuffled"])
@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
async def test_play_again_honours_the_enabled_dial(
    sync_db_path, enabled, button, monkeypatch
):
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    start = AsyncMock(return_value="new-gid")
    cog._start_new_game = start  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    await cog.db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, ?)",
        (GUILD, "clapback", int(enabled)),
    )
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    view = ClapbackRecapView("old-gid", HOST, config, cog.db, bot, cog)
    interaction = _interaction()

    await getattr(view, button).callback(interaction)  # type: ignore[arg-type]

    if enabled:
        start.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
    else:
        start.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "disabled" in interaction.response.send_message.await_args.args[0]
        # The recap card is left alone so the host can retry once it is back on.
        interaction.response.edit_message.assert_not_awaited()
