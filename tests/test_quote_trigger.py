"""Tests for the reply-to-quote trigger detection.

The listener fires on every message in the guild, so the guard conditions that
decide *whether* to render a card (reply present, the make_it_a_quote role
pinged, a quotable target) are pinned here. The render path itself is exercised
elsewhere and stubbed out.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.cogs.quote_cog as qc


def test_normalize_role_name_matches_variants():
    for variant in ("make_it_a_quote", "Make It A Quote", "  MAKE_IT_A_QUOTE  ", "make it a quote"):
        assert qc._normalize_role_name(variant) == qc._TRIGGER_ROLE_NAME
    assert qc._normalize_role_name("quotes") != qc._TRIGGER_ROLE_NAME


def _role(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _target(content: str = "hello world", msg_type=discord.MessageType.default):
    tgt = MagicMock(spec=discord.Message)
    tgt.content = content
    tgt.type = msg_type
    return tgt


def _message(*, bot=False, guild=True, reference=True, roles=None, target=None):
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=target)
    channel.id = 999
    ref = None
    if reference:
        ref = SimpleNamespace(message_id=123, resolved=target)
    return SimpleNamespace(
        author=SimpleNamespace(bot=bot),
        guild=object() if guild else None,
        reference=ref,
        role_mentions=roles if roles is not None else [_role("make_it_a_quote")],
        channel=channel,
    )


@pytest.fixture
def stub_render(monkeypatch):
    fake = AsyncMock(return_value=b"PNG")
    monkeypatch.setattr(qc, "_build_card_for_message", fake)
    return fake


async def _run(message):
    # The listener never touches ``self``; a bare object stands in for the cog.
    await qc.QuoteCog._on_quote_trigger(cast(qc.QuoteCog, object()), message)


async def test_triggers_on_reply_with_role_mention(stub_render):
    msg = _message(target=_target())
    await _run(msg)
    stub_render.assert_awaited_once()
    msg.channel.send.assert_awaited_once()


async def test_ignores_bot_author(stub_render):
    msg = _message(bot=True, target=_target())
    await _run(msg)
    stub_render.assert_not_awaited()
    msg.channel.send.assert_not_awaited()


async def test_ignores_without_reply(stub_render):
    msg = _message(reference=False, target=_target())
    await _run(msg)
    stub_render.assert_not_awaited()


async def test_ignores_without_trigger_role(stub_render):
    msg = _message(roles=[_role("some_other_role")], target=_target())
    await _run(msg)
    stub_render.assert_not_awaited()


async def test_ignores_empty_target(stub_render):
    msg = _message(target=_target(content="   "))
    await _run(msg)
    stub_render.assert_not_awaited()


async def test_ignores_system_target(stub_render):
    msg = _message(target=_target(msg_type=discord.MessageType.pins_add))
    await _run(msg)
    stub_render.assert_not_awaited()
