"""Cog-level: report-reply flow.

Targets the ``WhisperReportReplyModal.on_submit`` async handler, which was
the single largest uncovered block in whisper_cog.py (~55 statements). Each
test patches the DB shims so we can exercise the branches without spinning
up real SQLite.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import (
    Whisper,
    WhisperConfig,
    WhisperReply,
)
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET, OTHER = 1001, 2001, 9999
REPLY_ID = 55
WHISPER_ID = 42
GUILD_ID = 9001


def _w(**overrides) -> Whisper:
    defaults = dict(
        id=WHISPER_ID,
        guild_id=GUILD_ID,
        sender_id=SENDER,
        target_id=TARGET,
        message="hi",
        created_at=time.time(),
        state="pending",
        solved=False,
        exposed=False,
        guesses_left=3,
        channel_msg_id=88888,
        dm_msg_id=99999,
    )
    defaults.update(overrides)
    return Whisper(**defaults)  # type: ignore[arg-type]


def _reply(to_user_id: int = TARGET) -> WhisperReply:
    return WhisperReply(
        id=REPLY_ID,
        whisper_id=WHISPER_ID,
        from_user_id=SENDER,
        to_user_id=to_user_id,
        content="rude reply",
        created_at=time.time(),
    )


def _make_modal():
    from bot_modules.cogs.whisper_cog import WhisperReportReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportReplyModal(bot, reply_id=REPLY_ID)
    modal.reason_input._value = "spam"  # type: ignore[attr-defined]
    return modal


@pytest.mark.asyncio
async def test_report_reply_modal_errors_when_reply_missing():
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=None):
        await modal.on_submit(interaction)

    interaction.response.send_message.assert_awaited()
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_report_reply_modal_blocks_non_recipient():
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()):
        await modal.on_submit(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "recipient" in msg.lower()


@pytest.mark.asyncio
async def test_report_reply_modal_errors_when_parent_whisper_missing():
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await modal.on_submit(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "whisper not found" in msg.lower()


@pytest.mark.asyncio
async def test_report_reply_modal_duplicate_report_blocked():
    """If the DB rejects the insert (already reported), the user gets told."""
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch(
             "bot_modules.cogs.whisper_cog._do_insert_reply_report",
             return_value=False,
         ):
        await modal.on_submit(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "already reported" in msg.lower()


@pytest.mark.asyncio
async def test_report_reply_modal_happy_path_without_log_channel():
    """No log channel configured → report still persists, success message sent."""
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2, log_channel_id=0)

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch(
             "bot_modules.cogs.whisper_cog._do_insert_reply_report",
             return_value=True,
         ), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await modal.on_submit(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "moderators" in msg.lower()


@pytest.mark.asyncio
async def test_report_reply_modal_happy_path_with_log_channel():
    """Log channel configured → mod-log embed sent before success message."""
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction.guild = MagicMock()
    interaction.guild.get_channel.return_value = log_channel

    cfg = WhisperConfig(
        guild_id=GUILD_ID, role_id=1, channel_id=2, log_channel_id=12345,
    )

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch(
             "bot_modules.cogs.whisper_cog._do_insert_reply_report",
             return_value=True,
         ), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()
    sent_kwargs = log_channel.send.call_args.kwargs
    assert "embed" in sent_kwargs
    emb = sent_kwargs["embed"]
    assert emb.title == "🚨 Whisper Reply Reported"


@pytest.mark.asyncio
async def test_report_reply_modal_swallows_log_channel_http_error():
    """Mod-log post failures shouldn't bubble — user still sees success."""
    modal = _make_modal()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom"),
    )
    interaction.guild = MagicMock()
    interaction.guild.get_channel.return_value = log_channel

    cfg = WhisperConfig(
        guild_id=GUILD_ID, role_id=1, channel_id=2, log_channel_id=12345,
    )

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch(
             "bot_modules.cogs.whisper_cog._do_insert_reply_report",
             return_value=True,
         ), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await modal.on_submit(interaction)

    # Still confirms success to the reporter
    interaction.response.send_message.assert_awaited()
    msg = interaction.response.send_message.call_args.args[0]
    assert "moderators" in msg.lower()


@pytest.mark.asyncio
async def test_report_reply_button_blocks_non_recipient():
    """Test the button-level check (separate from the modal)."""
    from bot_modules.cogs.whisper_cog import WhisperReportReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReportReplyButton(bot, reply_id=REPLY_ID)

    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_reply_button_errors_when_reply_missing():
    from bot_modules.cogs.whisper_cog import WhisperReportReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReportReplyButton(bot, reply_id=REPLY_ID)

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=None):
        await button.callback(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "not found" in msg.lower()


@pytest.mark.asyncio
async def test_report_reply_button_opens_modal_for_recipient():
    from bot_modules.cogs.whisper_cog import WhisperReportReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReportReplyButton(bot, reply_id=REPLY_ID)

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_get_reply", return_value=_reply()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
