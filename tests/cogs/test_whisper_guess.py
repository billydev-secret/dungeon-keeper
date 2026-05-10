"""Cog-level: Guess button + modal flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.whisper_models import Whisper
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET = 1001, 2001


def _w(*, solved: bool = False, guesses_left: int = 3) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=0.0, state="pending", solved=solved, exposed=False,
        guesses_left=guesses_left, channel_msg_id=88888, dm_msg_id=99999,
    )


def _make_guess_button(whisper_id: int = 42):
    from cogs.whisper_cog import WhisperGuessButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperGuessButton(bot, whisper_id)


@pytest.mark.asyncio
async def test_guess_button_non_target_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "recipient" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_target_opens_modal():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_called_once()


@pytest.mark.asyncio
async def test_guess_modal_correct_marks_solved_and_posts_solved_message():
    from cogs.whisper_cog import WhisperGuessModal

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperGuessModal(bot, whisper_id=42)
    # discord.py's TextInput.value is a read-only property; set via _value.
    modal.guess_input._value = str(SENDER)  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.guild = MagicMock()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.guild.get_member = MagicMock(return_value=FakeMember(id=SENDER, display_name="Sender"))

    cfg_mock = MagicMock(channel_id=8001)
    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=cfg_mock), \
         patch("cogs.whisper_cog._do_record_guess") as rec:
        await modal.on_submit(interaction)

    rec.assert_called_once_with(":memory:", whisper_id=42, guessed_id=SENDER, correct=True)
    feed_channel.send.assert_awaited_once()
    sent = interaction.response.send_message.call_args
    assert "right" in sent.args[0].lower() or "correct" in sent.args[0].lower()


@pytest.mark.asyncio
async def test_guess_modal_wrong_decrements_only():
    from cogs.whisper_cog import WhisperGuessModal

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperGuessModal(bot, whisper_id=42)
    modal.guess_input._value = "9999"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.guild = MagicMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._do_record_guess") as rec:
        await modal.on_submit(interaction)

    rec.assert_called_once_with(":memory:", whisper_id=42, guessed_id=9999, correct=False)
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "wrong" in args[0].lower() or "left" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_modal_already_solved_rejects():
    from cogs.whisper_cog import WhisperGuessModal

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperGuessModal(bot, whisper_id=42)
    # discord.py's TextInput.value is a read-only property; set via _value.
    modal.guess_input._value = str(SENDER)  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(solved=True)), \
         patch("cogs.whisper_cog._do_record_guess") as rec:
        await modal.on_submit(interaction)

    rec.assert_not_called()
