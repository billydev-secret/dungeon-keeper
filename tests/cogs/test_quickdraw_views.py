"""Tests for quickdraw views."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.quickdraw.views import FireView
from tests.fakes import FakeUser, fake_interaction


# ── FireView ──────────────────────────────────────────────────────────────────

async def test_fire_view_callback_fires():
    on_fire = AsyncMock()
    view = FireView(game_id=5, on_fire=on_fire)

    interaction = fake_interaction(user=FakeUser(id=10))
    fire_btn = view.children[0]
    assert isinstance(fire_btn, discord.ui.Button)
    await fire_btn.callback(interaction)

    on_fire.assert_awaited_once_with(interaction, 5)


def test_fire_view_custom_id_encodes_game_id():
    view = FireView(game_id=42, on_fire=AsyncMock())
    btn = view.children[0]
    assert btn.custom_id == "fire:42"


def test_fire_view_label():
    view = FireView(game_id=1, on_fire=AsyncMock())
    btn = view.children[0]
    assert "Fire" in btn.label


def test_fire_view_style_is_danger():
    view = FireView(game_id=1, on_fire=AsyncMock())
    btn = view.children[0]
    assert btn.style == discord.ButtonStyle.danger


def test_fire_view_disable_disables_button():
    view = FireView(game_id=1, on_fire=AsyncMock())
    view.disable()
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


def test_fire_view_single_button():
    view = FireView(game_id=7, on_fire=AsyncMock())
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1


async def test_fire_view_different_game_ids_have_different_custom_ids():
    view1 = FireView(game_id=1, on_fire=AsyncMock())
    view2 = FireView(game_id=2, on_fire=AsyncMock())
    assert view1.children[0].custom_id != view2.children[0].custom_id
