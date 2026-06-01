"""Cog-level: WhisperInboxSelectView callback coverage + share-side-effects.

Covers the async event-handler branches of ``WhisperInboxSelectView``
(_on_select, _on_prev, _on_next, _on_filter, _on_clear_filter, _on_share,
_on_delete, _on_guess, _on_reply, _on_report) and the helper
``_share_side_effects``. These are the densest uncovered async-handler
clusters left in ``whisper_cog.py`` after the pure-logic extraction.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import (
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperConfig,
)
from tests.fakes import FakeMember, fake_interaction

SENDER = 1001
TARGET = 2001
OTHER = 9999
GUILD_ID = 9001


def _w(**overrides) -> Whisper:
    defaults = dict(
        id=1,
        guild_id=GUILD_ID,
        sender_id=SENDER,
        target_id=TARGET,
        message="hi",
        created_at=time.time(),
        state=STATE_PENDING,
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


def _view(whispers: list[Whisper], *, mode: str = "received"):
    from bot_modules.cogs.whisper_cog import WhisperInboxSelectView
    return WhisperInboxSelectView(
        _bot(), whispers, invoker_id=TARGET, mode=mode,
    )


# ── interaction_check ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interaction_check_blocks_other_user():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    assert (await view.interaction_check(interaction)) is False
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_interaction_check_allows_invoker():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    assert (await view.interaction_check(interaction)) is True


# ── nav / select callbacks ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_select_updates_selected_id():
    ws = [_w(id=1), _w(id=2), _w(id=3)]
    view = _view(ws)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    interaction.data = {"values": ["2"]}
    await view._on_select(interaction)
    assert view._selected_id == 2
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_on_select_ignores_none_sentinel():
    ws = [_w(id=1)]
    view = _view(ws)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    interaction.data = {"values": ["__none__"]}
    await view._on_select(interaction)
    # selection unchanged
    assert view._selected_id == 1


@pytest.mark.asyncio
async def test_on_prev_decrements_page():
    ws = [_w(id=i) for i in range(1, 40)]
    view = _view(ws)
    view._page = 1
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    await view._on_prev(interaction)
    assert view._page == 0


@pytest.mark.asyncio
async def test_on_prev_clamps_at_zero():
    ws = [_w(id=i) for i in range(1, 40)]
    view = _view(ws)
    view._page = 0
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    await view._on_prev(interaction)
    assert view._page == 0


@pytest.mark.asyncio
async def test_on_next_increments_page():
    ws = [_w(id=i) for i in range(1, 40)]
    view = _view(ws)
    view._page = 0
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    await view._on_next(interaction)
    assert view._page == 1


@pytest.mark.asyncio
async def test_on_filter_opens_modal():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    await view._on_filter(interaction)
    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_clear_filter_resets_display_and_selection():
    ws = [_w(id=1), _w(id=2)]
    view = _view(ws)
    view._filter_query = "foo"
    view._display = []  # simulate filtered-empty state
    view._selected_id = 999  # invalid
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    await view._on_clear_filter(interaction)
    assert view._filter_query == ""
    assert view._display == ws
    assert view._selected_id == 1  # falls back to first row


# ── action callbacks ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_share_blocks_invalid_transition():
    view = _view([_w(state=STATE_SHARED)])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_share(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "already" in msg


@pytest.mark.asyncio
async def test_on_share_happy_path_updates_state():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_update_state") as upd, \
         patch(
             "bot_modules.cogs.whisper_cog._share_side_effects",
             new=AsyncMock(),
         ):
        await view._on_share(interaction)

    upd.assert_called_once()
    assert view._all[0].state == STATE_SHARED
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_on_share_no_selection_short_circuits():
    view = _view([])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_share(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_delete_removes_whisper():
    ws = [_w(id=1), _w(id=2)]
    view = _view(ws)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_soft_delete"):
        await view._on_delete(interaction)

    assert all(w.id != 1 for w in view._all)
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_on_delete_no_selection_short_circuits():
    view = _view([])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_delete(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_guess_no_selection_short_circuits():
    view = _view([])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_guess(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_reply_no_selection_short_circuits():
    view = _view([])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_reply(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_reply_opens_modal_when_allowed():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=0):
        await view._on_reply(interaction)
    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_reply_blocks_when_cap_hit():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    with patch("bot_modules.cogs.whisper_cog._do_count_replies", return_value=1):
        await view._on_reply(interaction)
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_on_report_no_selection_short_circuits():
    view = _view([])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_report(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_report_blocks_non_target():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    # interaction_check normally blocks but we're calling _on_report directly
    await view._on_report(interaction)
    interaction.response.send_modal.assert_not_called()


@pytest.mark.asyncio
async def test_on_report_opens_modal_for_target():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    await view._on_report(interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── _share_side_effects ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_side_effects_no_guild_returns_early():
    from bot_modules.cogs.whisper_cog import _share_side_effects
    bot = _bot()
    bot.get_guild.return_value = None
    await _share_side_effects(bot, _w())
    # No DB call since we returned early
    bot.get_guild.assert_called_once()


@pytest.mark.asyncio
async def test_share_side_effects_no_feed_channel_returns_early():
    from bot_modules.cogs.whisper_cog import _share_side_effects
    bot = _bot()
    guild = MagicMock()
    guild.get_channel.return_value = None  # no feed channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)
    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await _share_side_effects(bot, _w())


@pytest.mark.asyncio
async def test_share_side_effects_happy_path_posts_new_message():
    from bot_modules.cogs.whisper_cog import _share_side_effects
    bot = _bot()
    feed_channel = MagicMock(spec=discord.TextChannel)
    old_msg = MagicMock()
    old_msg.delete = AsyncMock()
    feed_channel.fetch_message = AsyncMock(return_value=old_msg)
    new_msg = MagicMock(id=77777)
    feed_channel.send = AsyncMock(return_value=new_msg)
    guild = MagicMock()
    guild.get_channel.return_value = feed_channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids") as set_ids:
        await _share_side_effects(bot, _w(channel_msg_id=12345))

    feed_channel.send.assert_awaited_once()
    set_ids.assert_called_once()


@pytest.mark.asyncio
async def test_share_side_effects_swallows_delete_http_error():
    from bot_modules.cogs.whisper_cog import _share_side_effects
    bot = _bot()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.fetch_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom"),
    )
    new_msg = MagicMock(id=77777)
    feed_channel.send = AsyncMock(return_value=new_msg)
    guild = MagicMock()
    guild.get_channel.return_value = feed_channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids"):
        await _share_side_effects(bot, _w(channel_msg_id=12345))

    # send still tried after the delete error
    feed_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_share_side_effects_swallows_send_http_error():
    from bot_modules.cogs.whisper_cog import _share_side_effects
    bot = _bot()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom"),
    )
    guild = MagicMock()
    guild.get_channel.return_value = feed_channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids") as set_ids:
        # no channel_msg_id this time, so no old-msg delete attempt
        await _share_side_effects(bot, _w(channel_msg_id=0))

    set_ids.assert_not_called()
