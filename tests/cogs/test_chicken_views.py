"""Tests for chicken/views.py (ChickenView)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.chicken.views import ChickenView


def test_chicken_view_single_bail_button():
    view = ChickenView(6, AsyncMock())
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1
    assert buttons[0].custom_id == "chicken_bail:6"


def test_chicken_view_persistent():
    assert ChickenView(1, AsyncMock()).timeout is None


def test_chicken_view_disable():
    view = ChickenView(1, AsyncMock())
    view.disable()
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


async def test_bail_button_callback_invokes_handler():
    handler = AsyncMock()
    view = ChickenView(4, handler)
    button = next(c for c in view.children if isinstance(c, discord.ui.Button))
    interaction = object()
    await button.callback(interaction)  # type: ignore[arg-type]
    handler.assert_awaited_once_with(interaction, 4)
