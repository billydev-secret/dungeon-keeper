"""Wiring for the ``/games help`` panel (platform-25 / discovery-7).

The panel's pure halves are pinned in ``tests/test_games_help_logic.py``;
this file covers the one seam that touches live state — the Start button
launching through the shared launch guard — plus the thin glue: the command
sends one ephemeral message carrying the view, the select swaps the card,
Back restores the overview.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from bot_modules.cogs import games_help_cog
from bot_modules.games.utils import game_manager
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.utils.launch_guard import CHANNEL_NOT_ALLOWED_MSG
from bot_modules.games_help import views as help_views
from bot_modules.games_help.views import GameHelpView, start_from_help
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 780
HOST = 1
MSG = 999_001


def _bot(db_path, launcher=None):
    return SimpleNamespace(
        games_db=GamesDb(db_path),
        game_launchers={"wyr": launcher} if launcher else {},
        ctx=SimpleNamespace(db_path=db_path),
    )


def _channel():
    return SimpleNamespace(id=CHAN, name="games", guild=None, is_nsfw=lambda: False, send=AsyncMock())


async def _allow(db):
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (CHAN, GUILD),
    )
    await db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text) VALUES ('wyr', 'sfw', 'a | b')",
    )


async def _start(bot, channel_id=CHAN):
    return await start_from_help(
        bot, game_type="wyr", channel=_channel(), channel_id=channel_id,
        guild_id=GUILD, user_id=HOST, user_name="Host",
    )


async def test_start_refuses_a_busy_channel_and_never_launches(sync_db_path):
    launch = AsyncMock(return_value="gid")
    bot = _bot(sync_db_path, launch)
    await _allow(bot.games_db)
    await create_game(bot.games_db, CHAN, 7, "clapback", message_id=MSG, guild_id=GUILD)

    started, message = await _start(bot)

    assert started is False
    launch.assert_not_awaited()
    assert "already a game running" in message
    assert "Clapback" in message
    assert f"https://discord.com/channels/{GUILD}/{CHAN}/{MSG}" in message


async def test_start_refuses_a_channel_off_the_allowlist(sync_db_path):
    launch = AsyncMock(return_value="gid")
    bot = _bot(sync_db_path, launch)

    started, message = await _start(bot)

    assert (started, message) == (False, CHANNEL_NOT_ALLOWED_MSG)
    launch.assert_not_awaited()


async def test_start_launches_as_the_member_when_clear(sync_db_path, monkeypatch):
    sign_off = AsyncMock()
    monkeypatch.setattr(help_views, "sign_off_game_chore", sign_off)
    launch = AsyncMock(return_value="gid")
    bot = _bot(sync_db_path, launch)
    await _allow(bot.games_db)

    started, message = await _start(bot)

    assert started is True
    assert "Would You Rather" in message
    launch.assert_awaited_once()
    assert launch.await_args is not None
    kwargs = launch.await_args.kwargs
    assert kwargs["host_id"] == HOST and kwargs["guild_id"] == GUILD
    assert kwargs["options"] == {}
    sign_off.assert_awaited_once_with(bot, GUILD, HOST)


async def test_start_reports_a_failed_launch_with_the_perms_hint(sync_db_path):
    bot = _bot(sync_db_path, AsyncMock(return_value=None))
    await _allow(bot.games_db)

    started, message = await _start(bot)

    assert started is False
    assert message == game_manager.DEFAULT_LAUNCH_PERMS_HINT


async def test_start_without_a_registered_launcher_is_a_hint_not_a_crash(sync_db_path):
    bot = _bot(sync_db_path)
    await _allow(bot.games_db)
    started, message = await _start(bot)
    assert (started, message) == (False, game_manager.DEFAULT_LAUNCH_PERMS_HINT)


# ── the glue ─────────────────────────────────────────────────────────


def _interaction(client=None) -> Any:
    return cast(Any, SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host"),
        guild=None,
        guild_id=GUILD,
        channel_id=CHAN,
        channel=_channel(),
        client=client,
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    ))


async def test_help_command_sends_one_ephemeral_panel(monkeypatch):
    monkeypatch.setattr(games_help_cog, "safe_resolve_accent", AsyncMock(return_value=None))
    interaction = _interaction()

    await games_help_cog.help_command.callback(interaction)  # type: ignore[attr-defined]

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], GameHelpView)
    assert kwargs["embed"].title and "Community Games" in kwargs["embed"].title


async def test_picking_a_game_swaps_the_card_and_offers_start():
    view = GameHelpView(color=None)
    interaction = _interaction()

    await view.show_game(interaction, "wyr")

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["embed"].title == "🤔 Would You Rather"
    labels = [getattr(item, "label", None) for item in view.children]
    assert "▶️ Start Here" in labels and "Back" in labels


async def test_picking_a_duel_offers_no_start_button():
    view = GameHelpView(color=None)
    await view.show_game(_interaction(), "pressure")
    labels = [getattr(item, "label", None) for item in view.children]
    assert "▶️ Start Here" not in labels and "Back" in labels


async def test_back_restores_the_overview():
    view = GameHelpView(color=None, extra_lines=["🏈 **Survivor** — open in <#5>"])
    await view.show_game(_interaction(), "wyr")
    interaction = _interaction()

    await view.back_button.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert "Community Games" in kwargs["embed"].title
    assert view.selected is None
    assert [getattr(i, "label", None) for i in view.children] == [None]


@pytest.mark.parametrize("started", [True, False])
async def test_start_button_edits_the_panel_or_keeps_it(monkeypatch, started):
    """A launch replaces the panel with the confirmation; a refusal is sent
    beside it so the member can pick something else."""
    fake = AsyncMock(return_value=(started, "msg"))
    monkeypatch.setattr(help_views, "start_from_help", fake)
    view = GameHelpView(color=None)
    view.selected = "wyr"
    view._build()
    interaction = _interaction(client=SimpleNamespace())

    await view.start_button.callback(interaction)

    interaction.response.defer.assert_awaited_once()
    assert fake.await_args is not None
    assert fake.await_args.kwargs["game_type"] == "wyr"
    if started:
        interaction.edit_original_response.assert_awaited_once_with(content="msg", embed=None, view=None)
        assert view.is_finished()
    else:
        interaction.followup.send.assert_awaited_once_with("msg", ephemeral=True)
        assert not view.is_finished()
