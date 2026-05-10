"""Cog-level: share / hide / expose flows."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.whisper_models import Whisper, WhisperConfig
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET = 1001, 2001
FEED, LOG = 8001, 8002


def _w(*, state: str = "pending", solved: bool = False) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=0.0, state=state, solved=solved, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=7001, channel_id=FEED, log_channel_id=LOG)


def _make_dm_view():
    from cogs.whisper_cog import WhisperDmView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperDmView(bot, 42)


def _make_expose_view():
    from cogs.whisper_cog import WhisperExposeView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperExposeView(bot, 42)


@pytest.mark.asyncio
async def test_share_target_pending_edits_feed_message():
    view = _make_dm_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.guild = MagicMock()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_msg = MagicMock()
    feed_msg.edit = AsyncMock()
    feed_channel.fetch_message = AsyncMock(return_value=feed_msg)
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_update_state") as upd:
        await view._on_share_click(interaction)

    upd.assert_called_once_with(":memory:", 42, "shared")
    feed_msg.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_share_non_pending_rejected():
    view = _make_dm_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(state="hidden")), \
         patch("cogs.whisper_cog._do_update_state") as upd:
        await view._on_share_click(interaction)

    upd.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_hide_target_pending_updates_state_no_edit():
    view = _make_dm_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._do_update_state") as upd:
        await view._on_hide_click(interaction)

    upd.assert_called_once_with(":memory:", 42, "hidden")


@pytest.mark.asyncio
async def test_expose_solved_target_edits_feed_message():
    view = _make_expose_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.content = "✅ You're Right!"
    interaction.message.edit = AsyncMock()
    interaction.guild = MagicMock()
    interaction.guild.get_member = MagicMock(return_value=FakeMember(id=SENDER, display_name="Sender"))

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(solved=True)), \
         patch("cogs.whisper_cog._do_mark_exposed") as mexp:
        await view._on_expose_click(interaction)

    mexp.assert_called_once()
    interaction.message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_expose_unsolved_rejected():
    view = _make_expose_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(solved=False)), \
         patch("cogs.whisper_cog._do_mark_exposed") as mexp:
        await view._on_expose_click(interaction)

    mexp.assert_not_called()


@pytest.mark.asyncio
async def test_expose_non_target_rejected():
    view = _make_expose_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(solved=True)), \
         patch("cogs.whisper_cog._do_mark_exposed") as mexp:
        await view._on_expose_click(interaction)

    mexp.assert_not_called()
