"""Cog-level: one-reply-per-whisper enforcement."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.whisper_models import Whisper
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET, OTHER = 1001, 2001, 9999


def _w() -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state="pending", solved=False, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )


def _make_button():
    from bot_modules.cogs.whisper_cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperReplyButton(bot, 42)


# ── Button-level gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_button_blocks_when_already_replied():
    """A second reply attempt errors at button click without opening the modal."""
    button = _make_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=1):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()
    args = interaction.response.send_message.call_args.args
    assert "already" in args[0].lower() or "limited" in args[0].lower()


@pytest.mark.asyncio
async def test_reply_button_allows_first_reply():
    button = _make_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_button_blocks_outsider_even_with_zero_replies():
    """Non-participant gets the participant error, not the cap error."""
    button = _make_button()
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()
    args = interaction.response.send_message.call_args.args
    assert "recipient" in args[0].lower() or "sender" in args[0].lower()


# ── Modal-level race protection ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_modal_recheck_blocks_second_submission():
    """If a reply lands between modal open and submit, on_submit still rejects."""
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "second reply"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=1), \
         patch("bot_modules.cogs.whisper_cog._do_insert_reply") as ins:
        await modal.on_submit(interaction)

    ins.assert_not_called()
    args = interaction.response.send_message.call_args.args
    assert "already" in args[0].lower() or "limited" in args[0].lower()


# ── Reply DM view no longer carries a Reply button ───────────────────────────


def test_reply_dm_view_omits_reply_button():
    """After the one allowed reply, the inbound DM view should NOT expose
    another Reply button — the chain ends here."""
    from bot_modules.cogs.whisper_cog import (
        WhisperReplyButton,
        WhisperReplyDmView,
    )
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperReplyDmView(bot, whisper_id=42, reply_id=99)
    types = {type(c) for c in view.children}
    assert WhisperReplyButton not in types
