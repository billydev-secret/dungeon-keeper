"""Cog-level: share / delete / expose flows."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import Whisper, WhisperConfig, WhisperState
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET = 1001, 2001
FEED, LOG = 8001, 8002
DM_MSG_ID = 99999


def _w(
    *,
    state: WhisperState = "pending",
    solved: bool = False,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state=state, solved=solved, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=DM_MSG_ID,
        deleted_at=deleted_at,
    )


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=7001, channel_id=FEED, log_channel_id=LOG)


def _make_share_button():
    from bot_modules.cogs.whisper_cog import WhisperShareButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperShareButton(bot, 42)


def _make_delete_button():
    from bot_modules.cogs.whisper_cog import WhisperDeleteButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperDeleteButton(bot, 42)


def _make_expose_button():
    from bot_modules.cogs.whisper_cog import WhisperExposeButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperExposeButton(bot, 42)


# ── Share ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_target_pending_deletes_old_and_posts_new_to_feed():
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.guild = MagicMock()
    interaction.message = None  # no DM-edit path in this test

    feed_channel = MagicMock(spec=discord.TextChannel)
    old_msg = MagicMock()
    old_msg.delete = AsyncMock()
    feed_channel.fetch_message = AsyncMock(return_value=old_msg)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=77777))
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_update_state") as upd, \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids") as set_ids:
        await button.callback(interaction)

    upd.assert_called_once_with(":memory:", 42, "shared")
    old_msg.delete.assert_awaited_once()
    feed_channel.send.assert_awaited_once()
    sent_content = feed_channel.send.call_args.args[0]
    assert "fresh Whisper was shared" in sent_content
    assert _w().message in sent_content
    set_ids.assert_called_once_with(
        ":memory:", 42, channel_msg_id=77777, dm_msg_id=DM_MSG_ID
    )


@pytest.mark.asyncio
async def test_share_non_pending_rejected():
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(state="shared")), \
         patch("bot_modules.cogs.whisper_cog._do_update_state") as upd:
        await button.callback(interaction)

    upd.assert_not_called()
    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_share_edits_dm_view_when_invoked_from_dm():
    """Share clicked from the DM (interaction.message.id == dm_msg_id) updates
    the DM view to drop Share/Delete, leaving just Guess."""
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.guild = MagicMock()

    feed_channel = MagicMock(spec=discord.TextChannel)
    old_feed_msg = MagicMock()
    old_feed_msg.delete = AsyncMock()
    feed_channel.fetch_message = AsyncMock(return_value=old_feed_msg)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=77777))
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)

    dm_msg = MagicMock()
    dm_msg.id = DM_MSG_ID
    dm_msg.edit = AsyncMock()
    interaction.message = dm_msg

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_update_state"), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids"):
        await button.callback(interaction)

    dm_msg.edit.assert_awaited_once()
    edited_view = dm_msg.edit.call_args.kwargs["view"]
    from bot_modules.cogs.whisper_cog import WhisperGuessButton, WhisperReplyButton
    button_types = [type(item) for item in edited_view.children]
    assert button_types == [WhisperGuessButton, WhisperReplyButton]


@pytest.mark.asyncio
async def test_share_from_inbox_leaves_other_message_alone():
    """Share clicked from the inbox dropdown (interaction.message.id != dm_msg_id)
    must NOT edit interaction.message — only the action runs."""
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.guild = MagicMock()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.fetch_message = AsyncMock()
    feed_channel.send = AsyncMock(return_value=MagicMock(id=77777))
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)

    other_msg = MagicMock()
    other_msg.id = 111111  # not the DM
    other_msg.edit = AsyncMock()
    interaction.message = other_msg

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_update_state"), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids"):
        await button.callback(interaction)

    other_msg.edit.assert_not_called()


# ── Delete ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_target_soft_deletes():
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.message = None

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_soft_delete") as sd:
        await button.callback(interaction)

    sd.assert_called_once_with(":memory:", 42)
    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "remov" in interaction.response.send_message.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_delete_non_target_rejected():
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=SENDER))  # sender, not target
    interaction.response.send_message = AsyncMock()
    interaction.message = None

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_soft_delete") as sd:
        await button.callback(interaction)

    sd.assert_not_called()


@pytest.mark.asyncio
async def test_delete_already_deleted_rejected():
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.message = None

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper",
               return_value=_w(deleted_at=1234.0)), \
         patch("bot_modules.cogs.whisper_cog._do_soft_delete") as sd:
        await button.callback(interaction)

    sd.assert_not_called()


@pytest.mark.asyncio
async def test_delete_from_dm_clears_dm_view():
    """Delete clicked from the DM clears all DM buttons (terminal)."""
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    dm_msg = MagicMock()
    dm_msg.id = DM_MSG_ID
    dm_msg.edit = AsyncMock()
    interaction.message = dm_msg

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_soft_delete"):
        await button.callback(interaction)

    dm_msg.edit.assert_awaited_once()
    assert dm_msg.edit.call_args.kwargs["view"] is None


@pytest.mark.asyncio
async def test_delete_from_inbox_leaves_other_message_alone():
    """Delete clicked from the inbox dropdown must NOT edit interaction.message."""
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    other_msg = MagicMock()
    other_msg.id = 111111
    other_msg.edit = AsyncMock()
    interaction.message = other_msg

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_soft_delete"):
        await button.callback(interaction)

    other_msg.edit.assert_not_called()


# ── Expose ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expose_solved_target_edits_feed_message():
    button = _make_expose_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.content = "✅ You're Right!"
    interaction.message.edit = AsyncMock()
    interaction.guild = MagicMock()
    interaction.guild.get_member = MagicMock(return_value=FakeMember(id=SENDER, display_name="Sender"))

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(solved=True)), \
         patch("bot_modules.cogs.whisper_cog._do_mark_exposed") as mexp:
        await button.callback(interaction)

    mexp.assert_called_once()
    interaction.message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_expose_unsolved_rejected():
    button = _make_expose_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(solved=False)), \
         patch("bot_modules.cogs.whisper_cog._do_mark_exposed") as mexp:
        await button.callback(interaction)

    mexp.assert_not_called()


@pytest.mark.asyncio
async def test_expose_non_target_rejected():
    button = _make_expose_button()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(solved=True)), \
         patch("bot_modules.cogs.whisper_cog._do_mark_exposed") as mexp:
        await button.callback(interaction)

    mexp.assert_not_called()
