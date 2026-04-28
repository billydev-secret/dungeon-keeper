"""Tests for Discord event handlers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

from cogs.events_cog import EventsCog, _collect_backfill_channels, _on_tree_error


# ── Helpers ───────────────────────────────────────────────────────────

def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.tree = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 1
    bot.get_guild = MagicMock(return_value=None)
    bot.guilds = []
    return bot


def _make_ctx(**kwargs) -> MagicMock:
    ctx = MagicMock()
    ctx.spoiler_required_channels = kwargs.get("spoiler_required_channels", set())
    ctx.bypass_role_ids = kwargs.get("bypass_role_ids", set())
    ctx.xp_excluded_channel_ids = kwargs.get("xp_excluded_channel_ids", set())
    ctx.xp_pair_states = kwargs.get("xp_pair_states", {})
    ctx.level_5_role_id = kwargs.get("level_5_role_id", 0)
    ctx.level_up_log_channel_id = kwargs.get("level_up_log_channel_id", 0)
    ctx.level_5_log_channel_id = kwargs.get("level_5_log_channel_id", 0)
    ctx.guild_id = kwargs.get("guild_id", 1)
    ctx.db_path = MagicMock()
    ctx.open_db = MagicMock()
    mock_conn = MagicMock()
    ctx.open_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
    ctx.open_db.return_value.__exit__ = MagicMock(return_value=False)
    return ctx


def _make_message(*, is_bot: bool = False, guild: Any = MagicMock(), channel_id: int = 10, author_id: int = 50, message_id: int = 1000) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    author = MagicMock(spec=discord.Member)
    author.bot = is_bot
    author.id = author_id
    author.display_name = f"user-{author_id}"
    msg.author = author
    msg.guild = guild
    msg.id = message_id
    channel = MagicMock()
    channel.id = channel_id
    msg.channel = channel
    msg.content = ""
    msg.system_content = ""
    msg.mentions = []
    msg.embeds = []
    msg.attachments = []
    msg.reference = None
    msg.type = discord.MessageType.default
    msg.created_at = MagicMock()
    msg.created_at.timestamp = MagicMock(return_value=1_000_000.0)
    return msg


def _make_interaction() -> MagicMock:
    ix = MagicMock(spec=discord.Interaction)
    ix.response.is_done = MagicMock(return_value=False)
    ix.response.send_message = AsyncMock()
    ix.guild_id = 1
    ix.user = MagicMock()
    ix.user.id = 100
    return ix


# ── on_message ────────────────────────────────────────────────────────

@pytest.fixture
def cog():
    return EventsCog(_make_bot(), _make_ctx())


@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_bot_message_ignored(mock_spoiler, mock_award, mock_activity, cog):
    mock_spoiler.return_value = False
    await cog.on_message(_make_message(is_bot=True))
    mock_activity.assert_not_called()
    mock_award.assert_not_called()


@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_dm_message_ignored(mock_spoiler, mock_award, mock_activity, cog):
    mock_spoiler.return_value = False
    await cog.on_message(_make_message(guild=None))
    mock_activity.assert_not_called()
    mock_award.assert_not_called()


@patch("cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.auto_delete_rule_exists", return_value=False)
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_spoiler_violation_stops_processing(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level, cog):
    mock_spoiler.return_value = True
    await cog.on_message(_make_message())
    mock_activity.assert_not_called()
    mock_award.assert_not_called()


@patch("cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.auto_delete_rule_exists", return_value=False)
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_normal_message_records_activity(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level, cog):
    mock_spoiler.return_value = False
    mock_award.return_value = None
    await cog.on_message(_make_message())
    mock_activity.assert_called_once()


@patch("cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("cogs.events_cog.track_auto_delete_message")
@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.auto_delete_rule_exists", return_value=True)
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_message_tracked_when_rule_exists(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_track, mock_level, cog):
    mock_spoiler.return_value = False
    mock_award.return_value = None
    await cog.on_message(_make_message(channel_id=10, message_id=999))
    mock_track.assert_called_once()
    args = mock_track.call_args[0]
    assert args[2] == 10
    assert args[3] == 999


@patch("cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.auto_delete_rule_exists", return_value=False)
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_xp_award_triggers_level_progress(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level, cog):
    mock_spoiler.return_value = False
    award_result = MagicMock()
    mock_award.return_value = award_result
    msg = _make_message()
    msg.author = MagicMock(spec=discord.Member)
    msg.author.bot = False
    msg.author.id = 50
    await cog.on_message(msg)
    mock_level.assert_awaited_once()


@patch("cogs.events_cog.store_message")
@patch("cogs.events_cog.record_member_activity")
@patch("cogs.events_cog.auto_delete_rule_exists", return_value=False)
@patch("cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_system_message_archives_system_content_without_activity_or_xp(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_store, cog):
    msg = _make_message()
    msg.content = ""
    msg.system_content = "bakedlays just showed up!"
    msg.type = discord.MessageType.new_member
    await cog.on_message(msg)
    mock_spoiler.assert_not_awaited()
    mock_activity.assert_not_called()
    mock_award.assert_not_awaited()
    assert mock_store.call_args.kwargs["content"] == "bakedlays just showed up!"


# ── on_ready ──────────────────────────────────────────────────────────

@patch("cogs.events_cog.asyncio.create_task")
async def test_on_ready_does_not_spawn_duplicate_backfill_task(mock_create_task):
    ctx = _make_ctx()
    bot = _make_bot()
    cog = EventsCog(bot, ctx)

    running_task = MagicMock()
    running_task.done.return_value = False

    def _capture_task(coro):
        coro.close()
        return running_task

    mock_create_task.side_effect = _capture_task
    await cog.on_ready()
    await cog.on_ready()
    mock_create_task.assert_called_once()


# ── on_raw_message_delete ─────────────────────────────────────────────

@patch("services.auto_delete_service.remove_tracked_auto_delete_message")
async def test_no_guild_id_ignored_on_delete(mock_remove):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawMessageDeleteEvent)
    payload.guild_id = None
    await cog.on_raw_message_delete(payload)
    mock_remove.assert_not_called()


@patch("services.auto_delete_service.remove_tracked_auto_delete_message")
async def test_with_guild_id_removes_message(mock_remove):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawMessageDeleteEvent)
    payload.guild_id = 1
    payload.channel_id = 10
    payload.message_id = 999
    await cog.on_raw_message_delete(payload)
    mock_remove.assert_called_once_with(ctx.db_path, 1, 10, 999)


# ── on_raw_bulk_message_delete ────────────────────────────────────────

@patch("services.auto_delete_service.remove_tracked_auto_delete_messages")
async def test_no_guild_id_ignored_on_bulk_delete(mock_remove):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawBulkMessageDeleteEvent)
    payload.guild_id = None
    await cog.on_raw_bulk_message_delete(payload)
    mock_remove.assert_not_called()


@patch("services.auto_delete_service.remove_tracked_auto_delete_messages")
async def test_with_guild_id_removes_messages(mock_remove):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawBulkMessageDeleteEvent)
    payload.guild_id = 1
    payload.channel_id = 10
    payload.message_ids = {100, 101, 102}
    await cog.on_raw_bulk_message_delete(payload)
    mock_remove.assert_called_once_with(ctx.db_path, 1, 10, {100, 101, 102})


# ── on_raw_reaction_add ───────────────────────────────────────────────

@patch("cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("cogs.events_cog.award_image_reaction_xp", new_callable=AsyncMock)
async def test_no_award_no_level_progress(mock_award, mock_level):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    mock_award.return_value = None
    payload = MagicMock(spec=discord.RawReactionActionEvent)
    await cog.on_raw_reaction_add(payload)
    mock_level.assert_not_awaited()


@patch("cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("cogs.events_cog.award_image_reaction_xp", new_callable=AsyncMock)
async def test_award_triggers_level_progress(mock_award, mock_level):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    member = MagicMock(spec=discord.Member)
    award_result = MagicMock()
    mock_award.return_value = (member, award_result)
    payload = MagicMock(spec=discord.RawReactionActionEvent)
    await cog.on_raw_reaction_add(payload)
    mock_level.assert_awaited_once()
    args, _ = mock_level.call_args
    assert args[0] == member
    assert args[1] == award_result
    assert args[2] == "image_reaction"


# ── _collect_backfill_channels ────────────────────────────────────────

def _make_channel(spec, channel_id: int, *, can_read: bool = True) -> MagicMock:
    ch = MagicMock(spec=spec)
    ch.id = channel_id
    perms = MagicMock()
    perms.read_message_history = can_read
    ch.permissions_for = MagicMock(return_value=perms)
    return ch


def _empty_async_iter():
    async def _gen():
        if False:
            yield None
    return _gen()


async def test_collect_backfill_includes_forum_voice_stage():
    """Forum threads, voice channels, and stage channels must be indexed.

    Regression: forum posts were never backfilled because the loop only
    walked guild.text_channels, so /delete_me silently missed them.
    """
    text = _make_channel(discord.TextChannel, 1)
    text.threads = []
    text.archived_threads = MagicMock(return_value=_empty_async_iter())

    forum_thread_active = _make_channel(discord.Thread, 200)
    forum_thread_archived = _make_channel(discord.Thread, 201)
    forum = _make_channel(discord.ForumChannel, 100)
    forum.threads = [forum_thread_active]

    async def _archived_forum(**_kwargs):
        yield forum_thread_archived
    forum.archived_threads = _archived_forum

    voice = _make_channel(discord.VoiceChannel, 300)
    stage = _make_channel(discord.StageChannel, 400)

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = [text]
    guild.forums = [forum]
    guild.voice_channels = [voice]
    guild.stage_channels = [stage]

    me = MagicMock(spec=discord.Member)
    result = await _collect_backfill_channels(guild, me)

    ids = {c.id for c in result}
    assert {1, 200, 201, 300, 400}.issubset(ids), (
        f"missing channels in backfill: got {ids}"
    )
    # Forum channel itself must NOT be in the list — it has no .history()
    assert 100 not in ids


async def test_collect_backfill_skips_unreadable_channels():
    text = _make_channel(discord.TextChannel, 1, can_read=False)
    text.threads = []
    text.archived_threads = MagicMock(return_value=_empty_async_iter())
    forum = _make_channel(discord.ForumChannel, 2, can_read=False)
    forum.threads = []

    async def _archived_forum(**_kwargs):
        if False:
            yield None
    forum.archived_threads = _archived_forum

    voice = _make_channel(discord.VoiceChannel, 3, can_read=False)
    stage = _make_channel(discord.StageChannel, 4, can_read=False)

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = [text]
    guild.forums = [forum]
    guild.voice_channels = [voice]
    guild.stage_channels = [stage]

    me = MagicMock(spec=discord.Member)
    result = await _collect_backfill_channels(guild, me)
    assert result == []


# ── on_app_command_error ──────────────────────────────────────────────

async def test_command_not_found_sends_ephemeral():
    ix = _make_interaction()
    error = app_commands.CommandNotFound("unknown_cmd", [])
    await _on_tree_error(ix, error)
    ix.response.send_message.assert_awaited_once()
    assert ix.response.send_message.call_args[1]["ephemeral"] is True
    assert "out of date" in ix.response.send_message.call_args[0][0].lower()


async def test_command_not_found_skipped_if_response_done():
    ix = _make_interaction()
    ix.response.is_done.return_value = True
    error = app_commands.CommandNotFound("cmd", [])
    await _on_tree_error(ix, error)
    ix.response.send_message.assert_not_awaited()


async def test_generic_error_sends_failure_message():
    ix = _make_interaction()
    error = app_commands.AppCommandError("something broke")
    await _on_tree_error(ix, error)
    ix.response.send_message.assert_awaited_once()
    assert ix.response.send_message.call_args[1]["ephemeral"] is True
    assert "failed" in ix.response.send_message.call_args[0][0].lower()
