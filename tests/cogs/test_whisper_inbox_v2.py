"""Cog-level: inbox v2 UI + reply + report flows."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.whisper_models import Whisper, WhisperConfig, WhisperState
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET, OTHER = 1001, 2001, 9999
FEED, LOG = 8001, 8002


def _w(
    *,
    wid: int = 42,
    state: WhisperState = "pending",
    sender_id: int = SENDER,
    target_id: int = TARGET,
    message: str = "hi there",
    created_at: float = 0.0,
) -> Whisper:
    return Whisper(
        id=wid,
        guild_id=9001,
        sender_id=sender_id,
        target_id=target_id,
        message=message,
        created_at=created_at,
        state=state,
        solved=False,
        exposed=False,
        guesses_left=3,
        channel_msg_id=88888,
        dm_msg_id=99999,
    )


def _cfg(*, log_channel_id: int = LOG) -> WhisperConfig:
    return WhisperConfig(
        guild_id=9001, role_id=7001, channel_id=FEED, log_channel_id=log_channel_id
    )


# ── _format_time_ago ─────────────────────────────────────────────────────────


def test_format_time_ago_seconds():
    from cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=30) == "30s ago"


def test_format_time_ago_minutes():
    from cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=125) == "2m ago"


def test_format_time_ago_hours():
    from cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=3600 * 4) == "4h ago"


def test_format_time_ago_days_plural_singular():
    from cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=86400) == "1 day ago"
    assert _format_time_ago(0, now=86400 * 7) == "7 days ago"


# ── _build_inbox ─────────────────────────────────────────────────────────────


def test_build_inbox_empty():
    from cogs.whisper_cog import _build_inbox
    bot = MagicMock()
    embed, view = _build_inbox(bot, [], title="Your Inbox", hidden_view=False)
    assert "(0)" in embed.title  # type: ignore[operator]
    assert "No whispers" in (embed.description or "")
    assert len(view.children) == 0


def test_build_inbox_shows_numbered_messages_and_buttons():
    from cogs.whisper_cog import (
        WhisperGuessButton,
        WhisperHideButton,
        WhisperReplyButton,
        WhisperReportButton,
        WhisperShareButton,
        _build_inbox,
    )
    bot = MagicMock()
    whispers = [_w(wid=10 + i, message=f"msg {i}") for i in range(3)]
    embed, view = _build_inbox(bot, whispers, title="Your Inbox", hidden_view=False)
    assert "**Message #1:**" in (embed.description or "")
    assert "**Message #3:**" in (embed.description or "")
    # 3 messages × 5 buttons = 15 items
    assert len(view.children) == 15
    button_types = {type(c) for c in view.children}
    assert button_types == {
        WhisperShareButton,
        WhisperHideButton,
        WhisperGuessButton,
        WhisperReplyButton,
        WhisperReportButton,
    }


def test_build_inbox_caps_at_five_visible_and_shows_hint_footer():
    from cogs.whisper_cog import _build_inbox
    bot = MagicMock()
    whispers = [_w(wid=10 + i) for i in range(7)]
    embed, view = _build_inbox(bot, whispers, title="Your Inbox", hidden_view=False)
    # 5 messages × 5 buttons = 25 items
    assert len(view.children) == 25
    assert embed.footer.text is not None
    assert "Last 5" in embed.footer.text
    assert "Hide old" in embed.footer.text
    assert "7 messages total" in embed.footer.text


def test_build_inbox_hidden_view_omits_hide_old_hint():
    from cogs.whisper_cog import _build_inbox
    bot = MagicMock()
    whispers = [_w(wid=10 + i) for i in range(7)]
    embed, view = _build_inbox(
        bot, whispers, title="Hidden Whispers", hidden_view=True
    )
    assert embed.footer.text is not None
    assert "Last 5" in embed.footer.text
    assert "Hide old" not in embed.footer.text


def test_build_inbox_assigns_unique_row_per_message():
    from cogs.whisper_cog import _build_inbox
    bot = MagicMock()
    whispers = [_w(wid=10 + i) for i in range(3)]
    _embed, view = _build_inbox(
        bot, whispers, title="Your Inbox", hidden_view=False
    )
    # First 5 buttons → row 0, next 5 → row 1, next 5 → row 2
    rows = [item.row for item in view.children]
    assert rows[0:5] == [0] * 5
    assert rows[5:10] == [1] * 5
    assert rows[10:15] == [2] * 5


# ── WhisperReplyButton + WhisperReplyModal ───────────────────────────────────


@pytest.mark.asyncio
async def test_reply_button_third_party_rejected():
    from cogs.whisper_cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReplyButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_reply_button_target_opens_modal():
    from cogs.whisper_cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReplyButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_called_once()


@pytest.mark.asyncio
async def test_reply_modal_target_dms_sender():
    from cogs.whisper_cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "thanks for the message"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._do_insert_reply") as ins:
        await modal.on_submit(interaction)

    sender_user.send.assert_awaited_once()
    ins.assert_called_once_with(
        ":memory:",
        whisper_id=42,
        from_user_id=TARGET,
        to_user_id=SENDER,
        content="thanks for the message",
    )
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_reply_modal_sender_dms_target():
    from cogs.whisper_cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "glad you liked it"  # type: ignore[attr-defined]

    target_user = MagicMock()
    target_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=target_user)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._do_insert_reply") as ins:
        await modal.on_submit(interaction)

    target_user.send.assert_awaited_once()
    ins.assert_called_once_with(
        ":memory:",
        whisper_id=42,
        from_user_id=SENDER,
        to_user_id=TARGET,
        content="glad you liked it",
    )


@pytest.mark.asyncio
async def test_reply_modal_dm_forbidden_does_not_persist():
    from cogs.whisper_cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "hi"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no dms")
    )
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._do_insert_reply") as ins:
        await modal.on_submit(interaction)

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "deliver" in args[0].lower() or "DMs" in args[0]


# ── WhisperReportButton + WhisperReportModal ─────────────────────────────────


@pytest.mark.asyncio
async def test_report_button_non_target_rejected():
    from cogs.whisper_cog import WhisperReportButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReportButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=SENDER))  # sender, not target
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_modal_posts_to_mod_log():
    from cogs.whisper_cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "creepy content"  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()
    sent_kwargs = log_channel.send.call_args.kwargs
    emb: discord.Embed = sent_kwargs["embed"]
    assert emb.title == "Whisper Reported"
    field_names = [f.name for f in emb.fields]
    assert "Sender" in field_names
    assert "Reporter (Target)" in field_names
    assert "Reason" in field_names
    assert "Whisper ID" in field_names
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_modal_no_log_channel_rejected():
    from cogs.whisper_cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "x"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg(log_channel_id=0)):
        await modal.on_submit(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "configured" in args[0].lower() or "log" in args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_empty_reason_uses_placeholder():
    from cogs.whisper_cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = ""  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    emb: discord.Embed = log_channel.send.call_args.kwargs["embed"]
    reason_field = next(f for f in emb.fields if f.name == "Reason")
    assert "no reason" in (reason_field.value or "").lower()


# ── B4: Report dedupe ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_modal_duplicate_rejected():
    """A second report from same reporter should be rejected without posting to mod log."""
    from cogs.whisper_cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "again"  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_insert_report", return_value=False):
        await modal.on_submit(interaction)

    log_channel.send.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "already" in args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_first_report_succeeds():
    """First report from a reporter should succeed and post to mod log."""
    from cogs.whisper_cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "bad"  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()


# ── S6: Reply mod log ─────────────────────────────────────────────────────────


# ── S2: Unhide button ────────────────────────────────────────────────────────


def test_build_inbox_hidden_view_uses_unhide_button():
    """hidden_view=True should yield WhisperUnhideButton instead of WhisperHideButton."""
    from cogs.whisper_cog import (
        WhisperHideButton,
        WhisperUnhideButton,
        _build_inbox,
    )
    bot = MagicMock()
    whispers = [_w(wid=10 + i) for i in range(2)]
    _embed, view = _build_inbox(bot, whispers, title="Hidden Whispers", hidden_view=True)
    button_types = {type(c) for c in view.children}
    assert WhisperUnhideButton in button_types
    assert WhisperHideButton not in button_types


@pytest.mark.asyncio
async def test_unhide_button_transitions_state_to_pending():
    """Unhide callback should update state from hidden -> pending."""
    from cogs.whisper_cog import WhisperUnhideButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperUnhideButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(state="hidden")), \
         patch("cogs.whisper_cog._do_update_state") as update_state:
        await button.callback(interaction)

    update_state.assert_called_once_with(":memory:", 42, "pending")
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_unhide_button_rejects_non_target():
    """Unhide callback rejects users who aren't the recipient."""
    from cogs.whisper_cog import WhisperUnhideButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperUnhideButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w(state="hidden")), \
         patch("cogs.whisper_cog._do_update_state") as update_state:
        await button.callback(interaction)

    update_state.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_reply_modal_posts_to_mod_log():
    """After a successful reply, a mod log embed should be posted."""
    from cogs.whisper_cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "anonymous reply text"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock()
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=log_channel)
    bot.get_guild = MagicMock(return_value=guild)

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("cogs.whisper_cog._do_insert_reply"), \
         patch("cogs.whisper_cog._load_config", return_value=_cfg()):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()
    emb: discord.Embed = log_channel.send.call_args.kwargs["embed"]
    assert emb.title == "Whisper Reply"
    field_names = [f.name for f in emb.fields]
    assert "From" in field_names
    assert "To" in field_names
    assert "Whisper ID" in field_names
