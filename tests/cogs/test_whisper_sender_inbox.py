"""Cog-level: sender's own inbox (/whisper sent + My Sent feed button)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.whisper_models import Whisper
from bot_modules.services.whisper_service import LOCK_DURATION_SECONDS
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET, OTHER_TARGET = 1001, 2001, 3003
NOW = time.time()


@pytest.fixture(autouse=True)
def _stub_accent_color(monkeypatch):
    """resolve_accent_color awaits guild.me.display_avatar.read(), which the
    mocked guilds here can't satisfy — stub it at the use-site namespace."""
    import discord

    monkeypatch.setattr(
        "bot_modules.cogs.whisper_cog.resolve_accent_color",
        AsyncMock(return_value=discord.Colour.default()),
    )


def _w(
    *,
    wid: int = 42,
    sender_id: int = SENDER,
    target_id: int = TARGET,
    created_at: float | None = None,
    solved: bool = False,
    exposed: bool = False,
    guesses_left: int = 3,
    state: str = "pending",
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=wid,
        guild_id=9001,
        sender_id=sender_id,
        target_id=target_id,
        message=f"secret {wid}",
        created_at=NOW if created_at is None else created_at,
        state=state,  # type: ignore[arg-type]
        solved=solved,
        exposed=exposed,
        guesses_left=guesses_left,
        channel_msg_id=88888,
        dm_msg_id=99999,
        deleted_at=deleted_at,
    )


# ── My Sent feed button ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_view_includes_my_sent_button():
    from bot_modules.cogs.whisper_cog import WhisperFeedView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperFeedView(bot)
    custom_ids = [getattr(c, "custom_id", None) for c in view.children]
    assert "whisper:check_sent" in custom_ids
    # "Check Hidden Whispers" must NOT be there any more.
    assert "whisper:check_hidden" not in custom_ids


@pytest.mark.asyncio
async def test_my_sent_button_filters_terminal_whispers():
    """Exposed / out-of-guesses-no-solve / age-locked are hidden from the
    sender's inbox view."""
    from bot_modules.cogs.whisper_cog import WhisperFeedView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperFeedView(bot)

    sent_rows = [
        _w(wid=10),  # pending → visible
        _w(wid=11, exposed=True),  # exposed → hidden
        _w(wid=12, guesses_left=0, solved=False),  # out of guesses → hidden
        _w(wid=13, solved=True),  # solved, not exposed yet → visible
        _w(wid=14, created_at=NOW - LOCK_DURATION_SECONDS - 1),  # locked → hidden
    ]

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_list_sent", return_value=sent_rows):
        await view._on_check_sent_click(interaction)

    interaction.response.send_message.assert_awaited_once()
    passed_view = interaction.response.send_message.call_args.kwargs["view"]
    visible_ids = {w.id for w in passed_view._all}
    assert visible_ids == {10, 13}


# ── /whisper sent slash command ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whisper_sent_slash_command_requires_guild():
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = None
    interaction.response.send_message = AsyncMock()

    await cog.whisper_sent.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    args = interaction.response.send_message.call_args.args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_whisper_sent_slash_command_opens_sent_inbox():
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    sent_rows = [_w(wid=10), _w(wid=11, target_id=OTHER_TARGET)]
    with patch("bot_modules.cogs.whisper_cog._do_list_sent", return_value=sent_rows):
        await cog.whisper_sent.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    sent_kwargs = interaction.response.send_message.call_args.kwargs
    assert sent_kwargs.get("ephemeral") is True
    view = sent_kwargs["view"]
    assert view._mode == "sent"
    assert {w.id for w in view._all} == {10, 11}


# ── Sent-mode embed surface differences ──────────────────────────────────────


def test_sent_mode_embed_titles_and_shows_target():
    """Sent mode embed should label the target and use the sent title."""
    from bot_modules.cogs.whisper_cog import WhisperInboxSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperInboxSelectView(
        bot, [_w(wid=42, target_id=OTHER_TARGET)],
        invoker_id=SENDER, mode="sent",
    )
    emb = view.embed()
    assert "Sent" in (emb.title or "")
    assert str(OTHER_TARGET) in (emb.description or "")


def test_sent_mode_empty_message_distinct_from_received():
    from bot_modules.cogs.whisper_cog import WhisperInboxSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperInboxSelectView(bot, [], invoker_id=SENDER, mode="sent")
    emb = view.embed()
    assert "haven" in (emb.description or "").lower() or "sent" in (emb.description or "").lower()
