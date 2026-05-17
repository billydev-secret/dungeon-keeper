"""Cog-level: persistent feed-channel buttons."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.whisper_models import Whisper, WhisperConfig, WhisperState
from tests.fakes import FakeMember, fake_interaction

ROLE = 7001


def _make_view():
    from bot_modules.cogs.whisper_cog import WhisperFeedView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperFeedView(bot)


def _w(state: WhisperState = "pending") -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=1001, target_id=2001, message="x",
        created_at=time.time(), state=state, solved=False, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=8001, log_channel_id=8002)


@pytest.mark.asyncio
async def test_send_whisper_button_sends_instructions():
    view = _make_view()
    interaction = fake_interaction(user=FakeMember(id=1001))
    interaction.response.send_message = AsyncMock()

    await view._on_send_click(interaction)

    interaction.response.send_message.assert_called_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_check_whispers_lists_pending_and_shared():
    view = _make_view()
    interaction = fake_interaction(user=FakeMember(id=2001))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_list_received_in_states", return_value=[_w("pending"), _w("shared")]):
        await view._on_check_click(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_view_registered_on_cog_load():
    """Persistent view must be added via bot.add_view at cog load so buttons survive restart."""
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    cog = WhisperCog(bot)
    await cog.cog_load()
    bot.add_view.assert_called()


@pytest.mark.asyncio
async def test_dynamic_buttons_registered_on_cog_load():
    """Per-whisper Guess/Share/Delete/Expose buttons must register as dynamic items so they survive bot restart."""
    from bot_modules.cogs.whisper_cog import (
        WhisperCog,
        WhisperDeleteButton,
        WhisperExposeButton,
        WhisperGuessButton,
        WhisperShareButton,
    )
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    cog = WhisperCog(bot)
    await cog.cog_load()
    bot.add_dynamic_items.assert_called_once()
    args = bot.add_dynamic_items.call_args.args
    assert WhisperGuessButton in args
    assert WhisperShareButton in args
    assert WhisperDeleteButton in args
    assert WhisperExposeButton in args
