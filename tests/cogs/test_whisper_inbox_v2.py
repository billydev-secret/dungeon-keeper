"""Cog-level: inbox v2 UI + reply + report flows."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import Whisper, WhisperConfig, WhisperState
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
    created_at: float | None = None,
    solved: bool = False,
    guesses_left: int = 3,
    exposed: bool = False,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=wid,
        guild_id=9001,
        sender_id=sender_id,
        target_id=target_id,
        message=message,
        created_at=time.time() if created_at is None else created_at,
        state=state,
        solved=solved,
        exposed=exposed,
        guesses_left=guesses_left,
        channel_msg_id=88888,
        dm_msg_id=99999,
        deleted_at=deleted_at,
    )


def _cfg(*, log_channel_id: int = LOG) -> WhisperConfig:
    return WhisperConfig(
        guild_id=9001, role_id=7001, channel_id=FEED, log_channel_id=log_channel_id
    )


# ── _format_time_ago ─────────────────────────────────────────────────────────


def test_format_time_ago_seconds():
    from bot_modules.cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=30) == "30s ago"


def test_format_time_ago_minutes():
    from bot_modules.cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=125) == "2m ago"


def test_format_time_ago_hours():
    from bot_modules.cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=3600 * 4) == "4h ago"


def test_format_time_ago_days_plural_singular():
    from bot_modules.cogs.whisper_cog import _format_time_ago
    assert _format_time_ago(0, now=86400) == "1 day ago"
    assert _format_time_ago(0, now=86400 * 7) == "7 days ago"


# ── WhisperInboxSelectView ───────────────────────────────────────────────────


def _make_inbox(whispers, *, mode: str = "received"):
    from bot_modules.cogs.whisper_cog import WhisperInboxSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperInboxSelectView(bot, whispers, invoker_id=TARGET, mode=mode)


def test_inbox_empty_view():
    view = _make_inbox([])
    emb = view.embed()
    assert "(0)" in (emb.title or "")
    assert "No whispers" in (emb.description or "")
    assert len(view.children) == 0


def test_inbox_renders_dropdown_and_action_row():
    """Three whispers should yield: 1 select (row 0), Filter btn (row 1),
    Share+Guess+Reply+Report+Delete (row 2). No pagination since <=25."""
    whispers = [_w(wid=10 + i, message=f"msg {i}") for i in range(3)]
    view = _make_inbox(whispers)
    rows = [item.row for item in view.children]
    # row 0: select
    assert rows.count(0) == 1
    # row 1: filter button only (no pagination)
    assert rows.count(1) == 1
    # row 2: action buttons (Guess, Share, Reply, Report, Delete = 5)
    assert rows.count(2) == 5


def test_inbox_select_options_use_status_and_preview():
    whispers = [
        _w(wid=10, message="first message", state="pending"),
        _w(wid=11, message="second message", state="shared"),
    ]
    view = _make_inbox(whispers)
    sel = next(c for c in view.children if isinstance(c, discord.ui.Select))
    labels = [o.label for o in sel.options]
    descriptions = [o.description for o in sel.options]
    assert any("New" in lbl for lbl in labels)
    assert any("Shared" in lbl for lbl in labels)
    assert "first message" in (descriptions[0] or "")


def test_inbox_paginates_past_25():
    whispers = [_w(wid=100 + i, message=f"m{i}") for i in range(30)]
    view = _make_inbox(whispers)
    rows = [item.row for item in view.children]
    # Pagination active: row 1 has ◀ ▶ Filter = 3 items
    assert rows.count(1) == 3
    # Select shows first 25 only
    sel = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert len(sel.options) == 25


def test_inbox_locked_whisper_omits_guess_button():
    from bot_modules.services.whisper_service import LOCK_DURATION_SECONDS
    old = _w(created_at=time.time() - LOCK_DURATION_SECONDS - 1)
    view = _make_inbox([old])
    # row 2 should be: Share + Reply + Report + Delete = 4 (no Guess)
    rows = [item.row for item in view.children]
    assert rows.count(2) == 4


def test_inbox_shared_whisper_omits_share_button():
    whispers = [_w(state="shared")]
    view = _make_inbox(whispers)
    # row 2: Guess + Reply + Report + Delete = 4 (no Share)
    rows = [item.row for item in view.children]
    assert rows.count(2) == 4


def test_inbox_exhausted_guesses_omits_guess_button():
    whispers = [_w(guesses_left=0)]
    view = _make_inbox(whispers)
    # row 2: Share + Reply + Report + Delete = 4 (no Guess)
    rows = [item.row for item in view.children]
    assert rows.count(2) == 4


def test_inbox_sent_mode_only_delete_action():
    """Sender's own inbox shows just the Delete action (no Share/Guess/Reply/Report)."""
    whispers = [_w(sender_id=TARGET, target_id=999, message="my secret")]
    view = _make_inbox(whispers, mode="sent")
    rows = [item.row for item in view.children]
    assert rows.count(2) == 1


def test_inbox_interaction_check_rejects_other_user():
    """Only the inbox owner can interact with the view."""
    view = _make_inbox([_w()])
    other = fake_interaction(user=FakeMember(id=OTHER))
    other.response.send_message = AsyncMock()
    import asyncio
    ok = asyncio.run(view.interaction_check(other))
    assert ok is False
    other.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_inbox_delete_removes_from_cache_and_redraws():
    view = _make_inbox([_w(wid=10), _w(wid=11)])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    # Selected starts at first whisper (id=10)
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(wid=10)), \
         patch("bot_modules.cogs.whisper_cog._do_soft_delete") as sd:
        await view._on_delete(interaction)
    sd.assert_called_once_with(":memory:", 10)
    assert len(view._all) == 1
    assert view._all[0].id == 11
    assert view._selected_id == 11
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbox_share_updates_cached_state_and_redraws():
    target_w = _w(wid=10, state="pending")
    view = _make_inbox([target_w])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=target_w), \
         patch("bot_modules.cogs.whisper_cog._do_update_state") as upd, \
         patch("bot_modules.cogs.whisper_cog._share_side_effects", AsyncMock()):
        await view._on_share(interaction)
    upd.assert_called_once_with(":memory:", 10, "shared")
    assert view._all[0].state == "shared"
    interaction.response.edit_message.assert_awaited_once()


# ── WhisperReplyButton + WhisperReplyModal ───────────────────────────────────


@pytest.mark.asyncio
async def test_reply_button_third_party_rejected():
    from bot_modules.cogs.whisper_cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReplyButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_reply_button_target_opens_modal():
    from bot_modules.cogs.whisper_cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReplyButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_called_once()


@pytest.mark.asyncio
async def test_reply_modal_target_dms_sender():
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0), \
         patch("bot_modules.cogs.whisper_cog._do_insert_reply", return_value=99) as ins:
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
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0), \
         patch("bot_modules.cogs.whisper_cog._do_insert_reply", return_value=99) as ins:
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
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0), \
         patch("bot_modules.cogs.whisper_cog._do_insert_reply", return_value=99), \
         patch("bot_modules.cogs.whisper_cog._do_delete_reply") as del_mock:
        await modal.on_submit(interaction)

    # Reply is inserted first (to get reply_id), then rolled back when DM fails.
    del_mock.assert_called_once_with(":memory:", 99)
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "deliver" in args[0].lower() or "DMs" in args[0]


# ── WhisperReportButton + WhisperReportModal ─────────────────────────────────


@pytest.mark.asyncio
async def test_report_button_non_target_rejected():
    from bot_modules.cogs.whisper_cog import WhisperReportButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReportButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=SENDER))  # sender, not target
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_modal_posts_to_mod_log():
    from bot_modules.cogs.whisper_cog import WhisperReportModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_report", return_value=True):
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
async def test_report_modal_records_even_when_no_log_channel():
    from bot_modules.cogs.whisper_cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "x"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(log_channel_id=0)), \
         patch("bot_modules.cogs.whisper_cog._do_insert_report", return_value=True) as ins:
        await modal.on_submit(interaction)

    # Report still gets recorded (visible on the web mod dashboard) even when
    # the Discord mod-log channel isn't set.
    ins.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "submitted" in args[0].lower() or "report" in args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_empty_reason_uses_placeholder():
    from bot_modules.cogs.whisper_cog import WhisperReportModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    emb: discord.Embed = log_channel.send.call_args.kwargs["embed"]
    reason_field = next(f for f in emb.fields if f.name == "Reason")
    assert "no reason" in (reason_field.value or "").lower()


# ── B4: Report dedupe ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_modal_duplicate_rejected():
    """A second report from same reporter should be rejected without posting to mod log."""
    from bot_modules.cogs.whisper_cog import WhisperReportModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_report", return_value=False):
        await modal.on_submit(interaction)

    log_channel.send.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "already" in args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_first_report_succeeds():
    """First report from a reporter should succeed and post to mod log."""
    from bot_modules.cogs.whisper_cog import WhisperReportModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()


# ── S6: Reply mod log ─────────────────────────────────────────────────────────


# ── S9: Reply DM identifies whisper id ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_modal_dm_includes_whisper_id():
    """The reply DM body must include 'Whisper #<id>' so recipient can map it."""
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "great question"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(wid=42)), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0), \
         patch("bot_modules.cogs.whisper_cog._do_insert_reply", return_value=99), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await modal.on_submit(interaction)

    sender_user.send.assert_awaited_once()
    sent_content = sender_user.send.call_args.args[0]
    assert "Whisper #42" in sent_content


@pytest.mark.asyncio
async def test_reply_modal_posts_to_mod_log():
    """After a successful reply, a mod log embed should be posted."""
    from bot_modules.cogs.whisper_cog import WhisperReplyModal
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

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0), \
         patch("bot_modules.cogs.whisper_cog._do_insert_reply", return_value=99), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()
    emb: discord.Embed = log_channel.send.call_args.kwargs["embed"]
    assert emb.title == "Whisper Reply"
    field_names = [f.name for f in emb.fields]
    assert "From" in field_names
    assert "To" in field_names
    assert "Whisper ID" in field_names
