"""Name Your Price's recap relaunch goes through the slash entry's gate.

The recap card's Run Again button called ``cog.launch`` directly, so it was
the one door with no guard on it: an admin could untick the game on the
dashboard and the host could keep it alive from the recap indefinitely — the
"toggle that isn't enforced" CLAUDE.md forbids. The guard itself
(``relaunch_refusal``) is shared with Rushmore and Clapback, so its branches
are pinned here once; the Rushmore file only proves its button is wired.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_modules.cogs.games_price_cog as cog_module
from bot_modules.cogs.games_price_cog import PriceCog, PriceRecapView
from bot_modules.games.utils.game_manager import create_game, relaunch_refusal
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 777
HOST = 1


async def _allow_channel(db: GamesDb, channel_id: int = CHAN) -> None:
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (channel_id, GUILD),
    )


async def _set_enabled(db: GamesDb, game_type: str, enabled: bool) -> None:
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, ?)",
        (GUILD, game_type, int(enabled)),
    )


# ── the shared guard ─────────────────────────────────────────────────────────


async def test_relaunch_is_allowed_when_every_check_passes(sync_db_path):
    db = GamesDb(sync_db_path)
    await _allow_channel(db)
    assert await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price") is None


async def test_relaunch_refuses_a_channel_games_may_not_run_in(sync_db_path):
    db = GamesDb(sync_db_path)
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price")
    assert msg is not None and "isn't set up for games" in msg


async def test_relaunch_refuses_when_the_dial_is_off(sync_db_path):
    db = GamesDb(sync_db_path)
    await _allow_channel(db)
    await _set_enabled(db, "price", False)
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price")
    assert msg == "Name Your Price is currently disabled on this server."


async def test_relaunch_refuses_when_another_game_is_running_here(sync_db_path):
    db = GamesDb(sync_db_path)
    await _allow_channel(db)
    await create_game(db, CHAN, 5, "wyr", state="playing", payload={})
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price")
    assert msg is not None and "already a game running" in msg


# ── the button is wired through it ───────────────────────────────────────────


def _interaction(*, user_id: int = HOST):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Host"),
        guild_id=GUILD,
        channel_id=CHAN,
        channel=SimpleNamespace(id=CHAN, name="games", guild=None),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


def _cog(db_path) -> tuple[PriceCog, AsyncMock]:
    bot = SimpleNamespace(
        games_db=GamesDb(db_path), active_views={},
        ctx=SimpleNamespace(db_path=db_path),
    )
    cog = PriceCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    return cog, launch


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
async def test_run_again_honours_the_enabled_dial(sync_db_path, enabled, monkeypatch):
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    cog, launch = _cog(sync_db_path)
    await _allow_channel(cog.db)
    await _set_enabled(cog.db, "price", enabled)
    view = PriceRecapView("old-gid", HOST, cog, {"rounds": 3})
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
        # The recap card is left alone so the host can retry once it is back on.
        interaction.message.edit.assert_not_awaited()
