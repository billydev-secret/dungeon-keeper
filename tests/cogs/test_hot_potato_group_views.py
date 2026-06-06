"""Tests for hot_potato_group/views.py (PassGroupView)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.hot_potato_group.views import PassGroupView


def test_pass_view_has_single_pass_button():
    view = PassGroupView(42, AsyncMock())
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1
    assert buttons[0].custom_id == "hpg_pass:42"
    assert buttons[0].emoji is not None and buttons[0].emoji.name == "🤲"


def test_pass_view_is_persistent():
    view = PassGroupView(1, AsyncMock())
    assert view.timeout is None


def test_pass_view_disable():
    view = PassGroupView(1, AsyncMock())
    view.disable()
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


async def test_pass_button_callback_invokes_handler():
    handler = AsyncMock()
    view = PassGroupView(99, handler)
    button = next(c for c in view.children if isinstance(c, discord.ui.Button))
    interaction = object()
    await button.callback(interaction)  # type: ignore[arg-type]
    handler.assert_awaited_once_with(interaction, 99)
