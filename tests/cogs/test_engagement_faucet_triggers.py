"""Unit tests for the confession/AMA economy-faucet trigger helpers.

The full Discord submit/approve flows can't run offline (live-tested via the
queue), but the small attribution helpers they call are pure enough to pin
here: who gets credited, the occurrence key, and the guild-resolution quirk
where AMA screened approval happens in the host's DMs (guild comes from the
game channel, not the interaction).
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import bot_modules.cogs.confessions_cog as cc
import bot_modules.cogs.games_ama_cog as ama
import bot_modules.economy.game_rewards as gr


async def test_confession_trigger_credits_confessor(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    interaction = types.SimpleNamespace(
        guild=types.SimpleNamespace(id=1),
        user=types.SimpleNamespace(id=42),
        client=object(),
    )
    await cc._fire_confession_trigger(interaction, occurrence="99")
    spy.assert_awaited_once()
    args, kwargs = spy.await_args
    assert args[1] == 1 and args[2] == 42 and args[3] == "confession"
    assert kwargs["occurrence"] == "99"


async def test_confession_trigger_skips_outside_guild(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    interaction = types.SimpleNamespace(
        guild=None, user=types.SimpleNamespace(id=42), client=object()
    )
    await cc._fire_confession_trigger(interaction, occurrence="99")
    spy.assert_not_awaited()


async def test_ama_ask_trigger_uses_channel_guild(monkeypatch):
    # Screened approval fires from the host's DMs, so the guild must come from
    # the game channel, not the interaction.
    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=7))
    await ama._fire_ama_ask_trigger(object(), channel, 42, "g1", 3)
    spy.assert_awaited_once()
    args, kwargs = spy.await_args
    assert args[1] == 7 and args[2] == 42 and args[3] == "ama_ask"
    assert kwargs["occurrence"] == "g1:3"


async def test_ama_ask_trigger_skips_without_guild(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    channel = object()  # no .guild attribute (e.g. a DM)
    await ama._fire_ama_ask_trigger(object(), channel, 42, "g1", 3)
    spy.assert_not_awaited()
