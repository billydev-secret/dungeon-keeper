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
    bot = object()
    await cc._fire_confession_trigger(bot, 1, 42, occurrence="99")
    spy.assert_awaited_once()
    args, kwargs = spy.await_args
    assert args[0] is bot
    assert args[1] == 1 and args[2] == 42 and args[3] == "confession"
    assert kwargs["occurrence"] == "99"


async def test_confession_trigger_credits_the_author_not_the_clicker(monkeypatch):
    """The ids are passed in, so mod-approve credits the right member.

    This replaces a test that fed the helper an ``interaction`` with no guild
    and asserted it stood down. That guard moved to the call sites when the
    helper started taking ids instead (`89709c7a`): under mod-approve the post
    is made by a moderator on the author's behalf, so the member who earns the
    quest is no longer the member who clicked, and the helper cannot work the
    author out from an interaction any more. What is worth pinning now is that
    it credits exactly the author id it was handed, and forwards the reply kind.
    """
    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    approver_clicked, confession_author = 7, 42
    await cc._fire_confession_trigger(
        object(), 1, confession_author,
        occurrence="99", kind="confession_reply",
    )
    spy.assert_awaited_once()
    args, kwargs = spy.await_args
    assert args[2] == confession_author != approver_clicked
    assert args[3] == "confession_reply"
    assert kwargs["occurrence"] == "99"


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
