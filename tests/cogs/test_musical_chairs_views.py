"""Tests for musical_chairs/views.py (SitView)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.musical_chairs.views import SitView


def test_sit_view_single_button():
    view = SitView(5, AsyncMock())
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1
    assert buttons[0].custom_id == "mc_sit:5"


def test_sit_view_persistent():
    assert SitView(1, AsyncMock()).timeout is None


def test_sit_view_disable():
    view = SitView(1, AsyncMock())
    view.disable()
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


async def test_sit_button_callback_invokes_handler():
    handler = AsyncMock()
    view = SitView(3, handler)
    button = next(c for c in view.children if isinstance(c, discord.ui.Button))
    interaction = object()
    await button.callback(interaction)  # type: ignore[arg-type]
    handler.assert_awaited_once_with(interaction, 3)
