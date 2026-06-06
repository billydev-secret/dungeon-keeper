"""Tests for hot_potato views."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.hot_potato.views import PassView
from tests.fakes import FakeUser, fake_interaction


# ── PassView ──────────────────────────────────────────────────────────────────

async def test_pass_view_callback_fires():
    on_pass = AsyncMock()
    view = PassView(game_id=5, on_pass=on_pass)

    interaction = fake_interaction(user=FakeUser(id=10))
    btn = view.children[0]
    assert isinstance(btn, discord.ui.Button)
    await btn.callback(interaction)

    on_pass.assert_awaited_once_with(interaction, 5)


def test_pass_view_custom_id_encodes_game_id():
    view = PassView(game_id=42, on_pass=AsyncMock())
    btn = view.children[0]
    assert btn.custom_id == "pass:42"


def test_pass_view_label_contains_pass():
    view = PassView(game_id=1, on_pass=AsyncMock())
    btn = view.children[0]
    assert "PASS" in btn.label


def test_pass_view_style_is_primary():
    view = PassView(game_id=1, on_pass=AsyncMock())
    btn = view.children[0]
    assert btn.style == discord.ButtonStyle.primary


def test_pass_view_disable_disables_button():
    view = PassView(game_id=1, on_pass=AsyncMock())
    view.disable()
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


def test_pass_view_single_button():
    view = PassView(game_id=7, on_pass=AsyncMock())
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1


async def test_pass_view_different_game_ids_have_different_custom_ids():
    view1 = PassView(game_id=1, on_pass=AsyncMock())
    view2 = PassView(game_id=2, on_pass=AsyncMock())
    assert view1.children[0].custom_id != view2.children[0].custom_id
