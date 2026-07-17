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
    for variant in (
        "make_it_a_quote",
        "Make It A Quote",
        "  MAKE_IT_A_QUOTE  ",
        "make it a quote",
        "MakeItaQuote",
        "MakeItAQuote",
    ):
        assert qc._normalize_role_name(variant) == qc._TRIGGER_ROLE_NAME
    assert qc._normalize_role_name("quotes") != qc._TRIGGER_ROLE_NAME


def _role(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _target(content: str = "hello world", msg_type=discord.MessageType.default, author_id=777):
    tgt = MagicMock(spec=discord.Message)
    tgt.content = content
    tgt.type = msg_type
    tgt.id = 123
    tgt.author = SimpleNamespace(id=author_id)
    return tgt


def _message(*, bot=False, guild=True, reference=True, roles=None, target=None, author_id=555):
    posted = MagicMock()
    posted.add_reaction = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=posted)
    channel.fetch_message = AsyncMock(return_value=target)
    channel.id = 999
    ref = None
    if reference:
        ref = SimpleNamespace(message_id=123, resolved=target)
    return SimpleNamespace(
        author=SimpleNamespace(bot=bot, id=author_id),
        guild=SimpleNamespace(id=1) if guild else None,
        reference=ref,
        role_mentions=roles if roles is not None else [_role("make_it_a_quote")],
        channel=channel,
    )


def _cog():
    """Stub cog exposing the .bot.ctx.open_db() context manager the star uses."""
    return SimpleNamespace(bot=SimpleNamespace(ctx=MagicMock()))


@pytest.fixture
def stub_render(monkeypatch):
    fake = AsyncMock(return_value=b"PNG")
    monkeypatch.setattr(qc, "_build_card_for_message", fake)
    # The default theme now resolves from guild branding (a DB read); these
    # detection tests use a stub bot/guild, so short-circuit it to a bundled theme.
    monkeypatch.setattr(
        qc, "_resolve_brand_theme", AsyncMock(return_value=qc.THEMES["golden_meadow"])
    )
    return fake


@pytest.fixture
def fire_spy(monkeypatch):
    """Patch the economy trigger so the credit path is exercised in isolation."""
    import bot_modules.economy.game_rewards as gr

    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    return spy


async def _run(message, cog=None):
    # Guard-path cases bail before touching ``self``; the happy path needs a cog
    # stub for the starboard lookup.
    await qc.QuoteCog._on_quote_trigger(cast(qc.QuoteCog, cog or object()), message)


async def test_triggers_on_reply_with_role_mention(stub_render, fire_spy, monkeypatch):
    monkeypatch.setattr(qc, "get_starboard_config", lambda conn, gid: None)
    msg = _message(target=_target())
    await _run(msg, cog=_cog())
    stub_render.assert_awaited_once()
    msg.channel.send.assert_awaited_once()
    # Auto-star: the posted card gets the default starboard reaction.
    msg.channel.send.return_value.add_reaction.assert_awaited_once_with("⭐")
    # Credits the quote creator (the invoker), keyed once per quoted message.
    fire_spy.assert_awaited_once()
    args, kwargs = fire_spy.await_args
    assert args[2] == 555 and args[3] == "quote"
    assert kwargs["occurrence"] == "123"


async def test_self_quote_does_not_credit(stub_render, fire_spy, monkeypatch):
    # Quoting your own message still renders the card but pays nothing (the
    # invoker has no rate limit, so self-quoting would be a trivial farm).
    monkeypatch.setattr(qc, "get_starboard_config", lambda conn, gid: None)
    msg = _message(author_id=555, target=_target(author_id=555))
    await _run(msg, cog=_cog())
    stub_render.assert_awaited_once()
    msg.channel.send.assert_awaited_once()
    fire_spy.assert_not_awaited()


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
