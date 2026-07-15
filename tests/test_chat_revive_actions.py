"""Tests for send_revive — the banner card and its plain-text fallbacks.

The card carries the question, but a role mention can't live inside an image,
so every path here checks that the ping survives whichever way we post.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.chat_revive import actions
from bot_modules.chat_revive.actions import send_revive
from bot_modules.services.quote_renderer import QUOTE_MAX_CHARS

ROLE_ID = 555
CARD_BYTES = b"\x89PNG-pretend"


class FakeAsset:
    def __init__(self, data: bytes | None) -> None:
        self._data = data

    def replace(self, **_kwargs) -> "FakeAsset":
        return self

    async def read(self) -> bytes:
        if self._data is None:
            raise discord.HTTPException(
                SimpleNamespace(status=500, reason="Internal Error"), "boom"
            )
        return self._data


class FakeChannel:
    def __init__(self, *, icon: FakeAsset | None) -> None:
        self.id = 200
        self.guild = SimpleNamespace(id=100, icon=icon)
        self.send = AsyncMock(return_value=SimpleNamespace(id=777))


@pytest.fixture
def rendered(monkeypatch) -> list[dict]:
    """Stub the renderer (PIL work is exercised by the quote tests)."""
    calls: list[dict] = []

    def _fake_render(text: str, **kwargs) -> bytes:
        calls.append({"text": text, **kwargs})
        return CARD_BYTES

    monkeypatch.setattr(actions, "render_quote_card", _fake_render)
    return calls


async def test_card_carries_question_and_content_carries_ping(rendered):
    channel = FakeChannel(icon=FakeAsset(b"icon"))

    await send_revive(
        channel, question_text="What's new?", role_id=ROLE_ID, flourish="*stirring…*"
    )

    assert rendered[0]["text"] == "What's new?"
    # The persona, not the feature name — a revive shouldn't announce itself.
    assert rendered[0]["author_name"] == "Ember"
    content = channel.send.await_args.args[0]
    assert content == "\U0001f525 *stirring…* <@&555>"
    file = channel.send.await_args.kwargs["file"]
    assert file.filename == actions.CARD_FILENAME
    # The whitelist is exactly the revive role — nothing else can be pinged.
    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert [r.id for r in allowed.roles] == [ROLE_ID]
    assert allowed.everyone is False


async def test_card_posts_bare_without_ping_or_flourish(rendered):
    channel = FakeChannel(icon=FakeAsset(b"icon"))

    await send_revive(channel, question_text="Q?", role_id=None, flourish=None)

    assert channel.send.await_args.args[0] is None
    assert channel.send.await_args.kwargs["file"] is not None


async def test_no_guild_icon_falls_back_to_text(rendered):
    channel = FakeChannel(icon=None)

    await send_revive(channel, question_text="Q?", role_id=ROLE_ID, flourish=None)

    assert not rendered
    assert channel.send.await_args.args[0] == "\U0001f525 <@&555> Q?"
    assert "file" not in channel.send.await_args.kwargs


async def test_unreadable_icon_falls_back_to_text(rendered):
    channel = FakeChannel(icon=FakeAsset(None))  # read() raises

    await send_revive(channel, question_text="Q?", role_id=None, flourish=None)

    assert not rendered
    assert channel.send.await_args.args[0] == "\U0001f525 Q?"


async def test_render_failure_falls_back_to_text(monkeypatch):
    def _boom(*_a, **_k) -> bytes:
        raise RuntimeError("PIL exploded")

    monkeypatch.setattr(actions, "render_quote_card", _boom)
    channel = FakeChannel(icon=FakeAsset(b"icon"))

    await send_revive(channel, question_text="Q?", role_id=ROLE_ID, flourish=None)

    assert channel.send.await_args.args[0] == "\U0001f525 <@&555> Q?"


async def test_overlong_question_falls_back_to_text(rendered):
    """The card trims at QUOTE_MAX_CHARS; text asks the question in full."""
    question = "x" * (QUOTE_MAX_CHARS + 1)
    channel = FakeChannel(icon=FakeAsset(b"icon"))

    await send_revive(channel, question_text=question, role_id=None, flourish=None)

    assert not rendered
    assert question in channel.send.await_args.args[0]
