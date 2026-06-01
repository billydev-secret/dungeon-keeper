"""Error-path coverage for per-whisper button callbacks.

The happy paths for ``WhisperGuessButton``, ``WhisperShareButton``,
``WhisperDeleteButton``, ``WhisperExposeButton``, ``WhisperReplyButton``,
and ``WhisperReportButton`` are covered in the existing cog tests. This
file adds the "whisper missing / wrong user / wrong state" branches that
were previously uncovered.

Each test patches the DB shim (``_do_load_whisper``) so we never hit
SQLite — what we're verifying is the conditional handling that sits
between the DB lookup and the next side effect.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.whisper_models import Whisper
from tests.fakes import FakeMember, fake_interaction

SENDER = 1001
TARGET = 2001
OTHER = 9999
WID = 42


def _w(**overrides) -> Whisper:
    defaults = dict(
        id=WID,
        guild_id=9001,
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


def _bot():
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return bot


# ── Guess button error branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guess_button_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperGuessButton
    btn = WhisperGuessButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await btn.callback(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_blocks_non_target():
    from bot_modules.cogs.whisper_cog import WhisperGuessButton
    btn = WhisperGuessButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await btn.callback(interaction)
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_guess_button_blocks_solved():
    from bot_modules.cogs.whisper_cog import WhisperGuessButton
    btn = WhisperGuessButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch(
        "bot_modules.cogs.whisper_cog._do_load_whisper",
        return_value=_w(solved=True),
    ):
        await btn.callback(interaction)
    interaction.response.send_message.assert_awaited()
    assert "solved" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_blocks_when_no_guesses_left():
    from bot_modules.cogs.whisper_cog import WhisperGuessButton
    btn = WhisperGuessButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch(
        "bot_modules.cogs.whisper_cog._do_load_whisper",
        return_value=_w(guesses_left=0),
    ):
        await btn.callback(interaction)
    assert "no more" in interaction.response.send_message.call_args.args[0].lower()


# ── Share button error branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_button_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperShareButton
    btn = WhisperShareButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await btn.callback(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_share_button_blocks_non_target():
    from bot_modules.cogs.whisper_cog import WhisperShareButton
    btn = WhisperShareButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await btn.callback(interaction)
    interaction.response.send_message.assert_awaited()


# ── Delete button error branches ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_button_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperDeleteButton
    btn = WhisperDeleteButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await btn.callback(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_delete_button_blocks_non_target():
    from bot_modules.cogs.whisper_cog import WhisperDeleteButton
    btn = WhisperDeleteButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await btn.callback(interaction)
    interaction.response.send_message.assert_awaited()


# ── Expose button error branches ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expose_button_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperExposeButton
    btn = WhisperExposeButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await btn.callback(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_expose_button_blocks_unsolved():
    from bot_modules.cogs.whisper_cog import WhisperExposeButton
    btn = WhisperExposeButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch(
        "bot_modules.cogs.whisper_cog._do_load_whisper",
        return_value=_w(solved=False),
    ):
        await btn.callback(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "solved" in msg


# ── Reply button error branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_button_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperReplyButton
    btn = WhisperReplyButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await btn.callback(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


# ── Report button error branches ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_button_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperReportButton
    btn = WhisperReportButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await btn.callback(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_report_button_blocks_non_target():
    from bot_modules.cogs.whisper_cog import WhisperReportButton
    btn = WhisperReportButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await btn.callback(interaction)
    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_button_opens_modal_for_target():
    from bot_modules.cogs.whisper_cog import WhisperReportButton
    btn = WhisperReportButton(_bot(), WID)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await btn.callback(interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── Reply modal error branches ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_modal_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
    modal = WhisperReplyModal(_bot(), whisper_id=WID)
    modal.reply_input._value = "x"  # type: ignore[attr-defined]
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await modal.on_submit(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_reply_modal_empty_content_rejected():
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
    modal = WhisperReplyModal(_bot(), whisper_id=WID)
    modal.reply_input._value = "   "  # whitespace only  # type: ignore[attr-defined]
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0):
        await modal.on_submit(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "empty" in msg


# ── Report modal error branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_modal_whisper_missing():
    from bot_modules.cogs.whisper_cog import WhisperReportModal
    modal = WhisperReportModal(_bot(), whisper_id=WID)
    modal.reason_input._value = "spam"  # type: ignore[attr-defined]
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=None):
        await modal.on_submit(interaction)
    assert "not found" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_blocks_non_target():
    from bot_modules.cogs.whisper_cog import WhisperReportModal
    modal = WhisperReportModal(_bot(), whisper_id=WID)
    modal.reason_input._value = "spam"  # type: ignore[attr-defined]
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await modal.on_submit(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "recipient" in msg


@pytest.mark.asyncio
async def test_report_modal_duplicate_blocked():
    from bot_modules.cogs.whisper_cog import WhisperReportModal
    modal = WhisperReportModal(_bot(), whisper_id=WID)
    modal.reason_input._value = "spam"  # type: ignore[attr-defined]
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch(
             "bot_modules.cogs.whisper_cog._do_insert_report", return_value=False,
         ):
        await modal.on_submit(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "already reported" in msg
