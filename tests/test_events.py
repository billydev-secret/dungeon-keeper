"""Tests for Discord event handlers."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

from bot_modules.cogs.events_cog import EventsCog, _collect_backfill_channels, _on_tree_error
from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import DEFAULT_XP_SETTINGS
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_quests_service import create_quest, set_quest_active
from bot_modules.services.economy_service import (
    get_balance,
    save_econ_settings,
)
from migrations import apply_migrations_sync


# ── Helpers ───────────────────────────────────────────────────────────

def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.tree = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 1
    bot.get_guild = MagicMock(return_value=None)
    bot.guilds = []
    # events_cog conditionally awaits bot.games_db.fetchall()
    bot.games_db = AsyncMock()
    bot.games_db.fetchall = AsyncMock(return_value=[])
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
    # on_message and reaction handlers read config via ctx.guild_config(gid);
    # return a stub carrying the same per-guild values the test passed in.
    _stub = _StubGuildConfig(
        spoiler_required_channels=kwargs.get("spoiler_required_channels", set()),
        bypass_role_ids=kwargs.get("bypass_role_ids", set()),
        recorded_bot_user_ids=kwargs.get("recorded_bot_user_ids", set()),
        xp_excluded_channel_ids=kwargs.get("xp_excluded_channel_ids", set()),
        level_5_role_id=kwargs.get("level_5_role_id", 0),
        level_5_log_channel_id=kwargs.get("level_5_log_channel_id", 0),
        level_up_log_channel_id=kwargs.get("level_up_log_channel_id", 0),
    )
    ctx.guild_config = MagicMock(return_value=_stub)
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


@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_bot_message_ignored(mock_spoiler, mock_award, mock_activity, cog):
    mock_spoiler.return_value = False
    await cog.on_message(_make_message(is_bot=True))
    mock_activity.assert_not_called()
    mock_award.assert_not_called()


@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_dm_message_ignored(mock_spoiler, mock_award, mock_activity, cog):
    mock_spoiler.return_value = False
    await cog.on_message(_make_message(guild=None))
    mock_activity.assert_not_called()
    mock_award.assert_not_called()


@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.should_track_auto_delete_message", return_value=False)
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_spoiler_violation_stops_processing(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level, cog):
    mock_spoiler.return_value = True
    await cog.on_message(_make_message())
    mock_activity.assert_not_called()
    mock_award.assert_not_called()


@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.should_track_auto_delete_message", return_value=False)
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_normal_message_records_activity(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level, cog):
    mock_spoiler.return_value = False
    mock_award.return_value = None
    await cog.on_message(_make_message())
    mock_activity.assert_called_once()


@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.track_auto_delete_message")
@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.should_track_auto_delete_message", return_value=True)
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_message_tracked_when_rule_exists(mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_track, mock_level, cog):
    mock_spoiler.return_value = False
    mock_award.return_value = None
    await cog.on_message(_make_message(channel_id=10, message_id=999))
    mock_track.assert_called_once()
    args = mock_track.call_args[0]
    assert args[2] == 10
    assert args[3] == 999


@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.should_track_auto_delete_message", return_value=False)
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
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


@patch("bot_modules.cogs.events_cog.store_message")
@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.should_track_auto_delete_message", return_value=False)
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
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

@patch("bot_modules.cogs.events_cog.asyncio.create_task")
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

@patch("bot_modules.cogs.events_cog.remove_tracked_auto_delete_message")
async def test_no_guild_id_ignored_on_delete(mock_remove):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawMessageDeleteEvent)
    payload.guild_id = None
    await cog.on_raw_message_delete(payload)
    mock_remove.assert_not_called()


@patch("bot_modules.cogs.events_cog.remove_tracked_auto_delete_message")
async def test_with_guild_id_clears_auto_delete_tracking(mock_remove):
    """Auto-delete tracking is per-message bookkeeping and is cleared, but the
    messages table itself is a permanent archive — see _archive_only test."""
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawMessageDeleteEvent)
    payload.guild_id = 1
    payload.channel_id = 10
    payload.message_id = 999
    await cog.on_raw_message_delete(payload)
    mock_remove.assert_called_once_with(ctx.db_path, 1, 10, 999)


async def test_message_archive_is_permanent_on_delete():
    """The messages table is never modified by on_raw_message_delete — the
    archive is preserved even after Discord forgets the message."""
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawMessageDeleteEvent)
    payload.guild_id = 1
    payload.channel_id = 10
    payload.message_id = 999
    with patch("bot_modules.cogs.events_cog.remove_tracked_auto_delete_message"):
        await cog.on_raw_message_delete(payload)
    # No DB connection should be opened to mutate the messages table.
    ctx.open_db.assert_not_called()


# ── on_raw_bulk_message_delete ────────────────────────────────────────

@patch("bot_modules.cogs.events_cog.remove_tracked_auto_delete_messages")
async def test_no_guild_id_ignored_on_bulk_delete(mock_remove):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawBulkMessageDeleteEvent)
    payload.guild_id = None
    await cog.on_raw_bulk_message_delete(payload)
    mock_remove.assert_not_called()


@patch("bot_modules.cogs.events_cog.remove_tracked_auto_delete_messages")
async def test_with_guild_id_clears_bulk_auto_delete_tracking(mock_remove):
    """Same as the single-delete case: clear tracking, but never touch the
    messages table itself (the archive is permanent)."""
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    payload = MagicMock(spec=discord.RawBulkMessageDeleteEvent)
    payload.guild_id = 1
    payload.channel_id = 10
    payload.message_ids = {100, 101, 102}
    await cog.on_raw_bulk_message_delete(payload)
    mock_remove.assert_called_once_with(ctx.db_path, 1, 10, {100, 101, 102})
    ctx.open_db.assert_not_called()


# ── on_raw_reaction_add ───────────────────────────────────────────────

@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.award_image_reaction_xp", new_callable=AsyncMock)
async def test_no_award_no_level_progress(mock_award, mock_level):
    ctx = _make_ctx()
    cog = EventsCog(_make_bot(), ctx)
    mock_award.return_value = None
    payload = MagicMock(spec=discord.RawReactionActionEvent)
    await cog.on_raw_reaction_add(payload)
    mock_level.assert_not_awaited()


@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.award_image_reaction_xp", new_callable=AsyncMock)
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


# ── on_member_join / on_member_remove per-guild config ────────────────
# Regression bait for the prelaunch P0 fix: welcome/leave/greeter channels
# must be read from ``ctx.guild_config(member.guild.id)``, not the home-guild
# flat fields on ctx. A 2nd guild that hasn't configured welcome must NOT
# inherit the home guild's settings via the legacy fallback.


class _StubGuildConfig:
    def __init__(
        self,
        *,
        welcome_channel_id: int = 0,
        welcome_message: str = "",
        welcome_ping_role_id: int = 0,
        welcome_ping_member: bool = False,
        welcome_trigger: str = "join",
        unverified_role_id: int = 0,
        greeter_chat_channel_id: int = 0,
        leave_channel_id: int = 0,
        leave_message: str = "",
        spoiler_required_channels: Any = None,
        bypass_role_ids: Any = None,
        recorded_bot_user_ids: Any = None,
        xp_excluded_channel_ids: Any = None,
        level_5_role_id: int = 0,
        level_5_log_channel_id: int = 0,
        level_up_log_channel_id: int = 0,
        grant_roles: Any = None,
        xp_settings: Any = DEFAULT_XP_SETTINGS,
        message_storage_level: str = "none",
        auto_role_ids: Any = None,
        greeting_watch_enabled: bool = False,
        greeting_watch_channel_ids: Any = None,
        greeting_watch_notify_user_id: int = 0,
        greeting_watch_window_minutes: int = 10,
    ):
        self.welcome_channel_id = welcome_channel_id
        self.welcome_message = welcome_message
        self.welcome_ping_role_id = welcome_ping_role_id
        self.welcome_ping_member = welcome_ping_member
        self.welcome_trigger = welcome_trigger
        self.unverified_role_id = unverified_role_id
        self.greeter_chat_channel_id = greeter_chat_channel_id
        self.leave_channel_id = leave_channel_id
        self.leave_message = leave_message
        self.spoiler_required_channels = (
            frozenset() if spoiler_required_channels is None else frozenset(spoiler_required_channels)
        )
        self.bypass_role_ids = (
            frozenset() if bypass_role_ids is None else frozenset(bypass_role_ids)
        )
        self.recorded_bot_user_ids = (
            frozenset() if recorded_bot_user_ids is None else frozenset(recorded_bot_user_ids)
        )
        self.xp_excluded_channel_ids = (
            frozenset() if xp_excluded_channel_ids is None else frozenset(xp_excluded_channel_ids)
        )
        self.level_5_role_id = level_5_role_id
        self.level_5_log_channel_id = level_5_log_channel_id
        self.level_up_log_channel_id = level_up_log_channel_id
        self.grant_roles = {} if grant_roles is None else grant_roles
        self.xp_settings = xp_settings
        self.message_storage_level = message_storage_level
        self.auto_role_ids = (
            frozenset() if auto_role_ids is None else frozenset(auto_role_ids)
        )
        self.greeting_watch_enabled = greeting_watch_enabled
        self.greeting_watch_channel_ids = (
            frozenset()
            if greeting_watch_channel_ids is None
            else frozenset(greeting_watch_channel_ids)
        )
        self.greeting_watch_notify_user_id = greeting_watch_notify_user_id
        self.greeting_watch_window_minutes = greeting_watch_window_minutes

    @property
    def retains_content(self) -> bool:
        return self.message_storage_level == "all"


def _make_member(*, guild_id: int = 1, member_id: int = 100, is_bot: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = is_bot
    member.display_name = f"user-{member_id}"
    member.mention = f"<@{member_id}>"
    guild = MagicMock()
    guild.id = guild_id
    guild.name = f"guild-{guild_id}"
    member.guild = guild
    member.created_at = MagicMock()
    member.created_at.timestamp = MagicMock(return_value=1_000_000.0)
    member.__str__ = MagicMock(return_value=f"user-{member_id}#0001")
    return member


def _patch_join_deps():
    """Patch the side-effectful imports inside on_member_join."""
    return [
        patch("bot_modules.cogs.events_cog.check_jail_rejoin", new_callable=AsyncMock),
        patch("bot_modules.cogs.events_cog.upsert_known_user"),
        patch("bot_modules.cogs.events_cog.record_member_event"),
        patch(
            "bot_modules.cogs.events_cog.detect_inviter",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("bot_modules.cogs.events_cog.build_welcome_embed", return_value="<embed>"),
    ]


def _patch_leave_deps():
    return [
        patch("bot_modules.cogs.events_cog.mark_member_left"),
        patch("bot_modules.cogs.events_cog.record_member_event"),
        patch("bot_modules.cogs.events_cog.build_leave_embed", return_value="<embed>"),
    ]


async def test_on_member_join_uses_per_guild_welcome_channel():
    """Welcome channel comes from cfg, not from ctx flat fields."""
    bot = _make_bot()
    ctx = _make_ctx()
    # ctx flat fields would point to a DIFFERENT channel; the cog must ignore them.
    ctx.welcome_channel_id = 999
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(welcome_channel_id=42, welcome_message="hi"),
    )
    cog = EventsCog(bot, ctx)

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.send = AsyncMock()
    member = _make_member(guild_id=7)
    member.guild.get_channel = MagicMock(return_value=welcome_channel)

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    ctx.guild_config.assert_called_with(7)  # per-guild lookup
    member.guild.get_channel.assert_any_call(42)  # NOT 999
    welcome_channel.send.assert_awaited_once()


async def test_on_member_join_skips_welcome_when_channel_unset():
    """Empty guild_config (e.g. unconfigured 2nd guild) → no welcome message sent."""
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.welcome_channel_id = 999  # home guild has one; this guild does not
    ctx.guild_config = MagicMock(return_value=_StubGuildConfig())  # all zeros
    cog = EventsCog(bot, ctx)

    member = _make_member(guild_id=20)
    member.guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    # get_channel was never asked about welcome_channel_id=0
    for call in member.guild.get_channel.call_args_list:
        assert call.args[0] != 999  # home guild's channel must NOT be used


async def test_on_member_join_includes_ping_role_when_set():
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(welcome_channel_id=42, welcome_ping_role_id=88),
    )
    cog = EventsCog(bot, ctx)

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.send = AsyncMock()
    member = _make_member()
    member.guild.get_channel = MagicMock(return_value=welcome_channel)

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    sent_kwargs = welcome_channel.send.call_args.kwargs
    assert sent_kwargs["content"] == "<@&88>"


async def test_on_member_join_omits_ping_when_role_unset():
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(welcome_channel_id=42, welcome_ping_role_id=0),
    )
    cog = EventsCog(bot, ctx)

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.send = AsyncMock()
    member = _make_member()
    member.guild.get_channel = MagicMock(return_value=welcome_channel)

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    sent_kwargs = welcome_channel.send.call_args.kwargs
    assert sent_kwargs["content"] is None


async def test_on_member_join_pings_member_when_enabled():
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(
            welcome_channel_id=42,
            welcome_ping_role_id=88,
            welcome_ping_member=True,
        ),
    )
    cog = EventsCog(bot, ctx)

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.send = AsyncMock()
    member = _make_member()
    member.guild.get_channel = MagicMock(return_value=welcome_channel)

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    sent_kwargs = welcome_channel.send.call_args.kwargs
    # Role ping and the member mention both ride in the content so the join
    # actually notifies the new member (mentions inside the embed do not).
    assert sent_kwargs["content"] == f"<@&88> {member.mention}"


async def test_on_member_join_pings_only_member_without_role():
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(
            welcome_channel_id=42,
            welcome_ping_role_id=0,
            welcome_ping_member=True,
        ),
    )
    cog = EventsCog(bot, ctx)

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.send = AsyncMock()
    member = _make_member()
    member.guild.get_channel = MagicMock(return_value=welcome_channel)

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    sent_kwargs = welcome_channel.send.call_args.kwargs
    assert sent_kwargs["content"] == member.mention


async def test_on_member_join_sends_greeter_ping():
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(greeter_chat_channel_id=77),
    )
    cog = EventsCog(bot, ctx)

    greeter_channel = MagicMock(spec=discord.TextChannel)
    greeter_channel.send = AsyncMock()
    member = _make_member(member_id=500)
    member.guild.get_channel = MagicMock(return_value=greeter_channel)

    with ExitStack() as stack:
        for p in _patch_join_deps():
            stack.enter_context(p)
        await cog.on_member_join(member)

    greeter_channel.send.assert_awaited_once_with("@here - <@500> has arrived")


def _role(role_id: int, name: str) -> MagicMock:
    role = MagicMock()
    role.id = role_id
    role.name = name
    return role


async def test_on_member_update_verified_fires_without_bio():
    """Stripping the unverified role fires the welcome with no bio required.

    Regression guard: DoubleCounter lifts the gate by removing the unverified
    role; the welcome must fire on that alone, independent of any bio.
    """
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(
            welcome_channel_id=42,
            welcome_trigger="verified",
            unverified_role_id=555,
        ),
    )
    cog = EventsCog(bot, ctx)
    cog._send_welcome = AsyncMock()

    member_role = _role(900, "Member")
    before = _make_member(guild_id=7)
    before.roles = [_role(555, "Unverified"), member_role]
    after = _make_member(guild_id=7)
    after.roles = [member_role]  # unverified role stripped

    with patch("bot_modules.cogs.events_cog.log_role_event"):
        await cog.on_member_update(before, after)

    cog._send_welcome.assert_awaited_once()
    assert cog._send_welcome.call_args.args[0] is after


async def test_on_member_update_verified_skips_when_unverified_role_kept():
    """No welcome while the unverified role is still present."""
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(
            welcome_channel_id=42,
            welcome_trigger="verified",
            unverified_role_id=555,
        ),
    )
    cog = EventsCog(bot, ctx)
    cog._send_welcome = AsyncMock()

    unverified = _role(555, "Unverified")
    before = _make_member(guild_id=7)
    before.roles = [unverified]
    after = _make_member(guild_id=7)
    after.roles = [unverified, _role(900, "Member")]  # gained a role, gate intact

    with patch("bot_modules.cogs.events_cog.log_role_event"):
        await cog.on_member_update(before, after)

    cog._send_welcome.assert_not_awaited()


async def test_on_member_remove_uses_per_guild_leave_channel():
    """Leave channel comes from cfg, not ctx flat fields."""
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.leave_channel_id = 999  # home guild
    ctx.guild_config = MagicMock(
        return_value=_StubGuildConfig(leave_channel_id=44, leave_message="bye"),
    )
    cog = EventsCog(bot, ctx)

    leave_channel = MagicMock(spec=discord.TextChannel)
    leave_channel.send = AsyncMock()
    member = _make_member(guild_id=7)
    member.guild.get_channel = MagicMock(return_value=leave_channel)

    with ExitStack() as stack:
        for p in _patch_leave_deps():
            stack.enter_context(p)
        await cog.on_member_remove(member)

    ctx.guild_config.assert_called_with(7)
    member.guild.get_channel.assert_called_with(44)  # NOT 999
    leave_channel.send.assert_awaited_once()


async def test_on_member_remove_skips_when_channel_unset():
    """No leave channel configured → no message sent; channel lookup not attempted."""
    bot = _make_bot()
    ctx = _make_ctx()
    ctx.leave_channel_id = 999  # home guild has one
    ctx.guild_config = MagicMock(return_value=_StubGuildConfig(leave_channel_id=0))
    cog = EventsCog(bot, ctx)

    member = _make_member(guild_id=20)
    member.guild.get_channel = MagicMock()

    with ExitStack() as stack:
        for p in _patch_leave_deps():
            stack.enter_context(p)
        await cog.on_member_remove(member)

    member.guild.get_channel.assert_not_called()


# ── economy: text login + QOTD hooks in _process_economy_message ──────────

ECON_GUILD = 4242
ECON_USER = 77
ECON_CHANNEL = 88


@pytest.fixture
def econ_db(tmp_path):
    db_path = tmp_path / "econ.db"
    apply_migrations_sync(db_path)
    return db_path


def _econ_cog(econ_db) -> EventsCog:
    ctx: Any = SimpleNamespace(db_path=econ_db, open_db=lambda: open_db(econ_db))
    return EventsCog(_make_bot(), ctx)


def _enable_econ(econ_db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(econ_db) as conn:
        save_econ_settings(conn, ECON_GUILD, values)


def _seed_streak(
    econ_db, *, streak: int, last_login_day: str, last_grace_day=None, shields=0
) -> None:
    with open_db(econ_db) as conn:
        conn.execute(
            """
            INSERT INTO econ_streaks
                (guild_id, user_id, current_streak, longest_streak,
                 last_login_day, last_grace_day, shields)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ECON_GUILD, ECON_USER, streak, streak, last_login_day,
                last_grace_day, shields,
            ),
        )


def _econ_message(
    *,
    booster: bool = False,
    user_id: int = ECON_USER,
    message_id: int | None = None,
    reply_to: int | None = None,
    content: str = "",
    role_mentions: tuple[int, ...] = (),
    admin: bool = False,
    manager_role_id: int | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    guild = MagicMock()
    guild.id = ECON_GUILD
    msg.guild = guild
    author = MagicMock(spec=discord.Member)
    author.id = user_id
    author.premium_since = object() if booster else None
    author.guild_permissions = SimpleNamespace(administrator=admin)
    author.roles = (
        [SimpleNamespace(id=manager_role_id)] if manager_role_id is not None else []
    )
    msg.author = author
    channel = MagicMock()
    channel.id = ECON_CHANNEL
    msg.channel = channel
    if message_id is not None:
        msg.id = message_id
    msg.content = content
    msg.role_mentions = [SimpleNamespace(id=r) for r in role_mentions]
    # Default to "not a reply" — a bare MagicMock reference would read as a
    # reply to a mock message id and reach the QOTD lookup as a non-integer.
    msg.reference = (
        SimpleNamespace(message_id=reply_to, resolved=None)
        if reply_to is not None
        else None
    )
    return msg


def _seed_qotd(econ_db, *, message_id: int, local_day: str | None = None) -> None:
    with open_db(econ_db) as conn:
        conn.execute(
            """
            INSERT INTO econ_qotd
                (guild_id, channel_id, message_id, question, posted_by, local_day, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ECON_GUILD, ECON_CHANNEL, message_id, "Q?", 1, local_day or _today(), 0.0),
        )


def _days_ago(n: int) -> str:
    import time as _t

    return local_day_for(_t.time() - n * 86400, 0.0)


def _today() -> str:
    import time as _t

    return local_day_for(_t.time(), 0.0)


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_disabled_is_noop(mock_notify, econ_db):
    cog = _econ_cog(econ_db)  # economy left disabled
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_not_awaited()
    with open_db(econ_db) as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM econ_logins").fetchone()
    assert rows["c"] == 0


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_first_login_dms_daily_digest(mock_notify, econ_db):
    _enable_econ(econ_db)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    # Every login now DMs a streak + quest digest (opt-in gated by
    # notify_member's require_game_role, mocked out here).
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    assert "day" in (embed.description or "").lower()
    with open_db(econ_db) as conn:
        assert get_balance(conn, ECON_GUILD, ECON_USER) > 0
        row = conn.execute(
            "SELECT current_streak FROM econ_streaks WHERE guild_id=? AND user_id=?",
            (ECON_GUILD, ECON_USER),
        ).fetchone()
    assert row["current_streak"] == 1


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_repeat_same_day_no_second_login(mock_notify, econ_db):
    _enable_econ(econ_db)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    with open_db(econ_db) as conn:
        first = get_balance(conn, ECON_GUILD, ECON_USER)
    await cog._process_economy_message(_econ_message())
    with open_db(econ_db) as conn:
        second = get_balance(conn, ECON_GUILD, ECON_USER)
    assert first == second  # process_login returns None on the same local day


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_milestone_dms(mock_notify, econ_db):
    _enable_econ(econ_db)
    _seed_streak(econ_db, streak=6, last_login_day=_days_ago(1))
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    assert any("milestone" in f.name.lower() for f in embed.fields)


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_grace_dms(mock_notify, econ_db):
    _enable_econ(econ_db)
    # Missed exactly one day, no grace used in the window → grace bridges it.
    _seed_streak(econ_db, streak=4, last_login_day=_days_ago(2))
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    assert any("saved" in f.name.lower() for f in embed.fields)


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_shield_consumed_dms_shield_copy(mock_notify, econ_db):
    _enable_econ(econ_db)
    # One missed day, but grace was burned inside the rolling window — only
    # the held shield saves the streak, and the digest must say so.
    _seed_streak(
        econ_db, streak=4, last_login_day=_days_ago(2),
        last_grace_day=_days_ago(4), shields=1,
    )
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    saved = next(f for f in embed.fields if "saved" in f.name.lower())
    assert "shield" in saved.value.lower()
    with open_db(econ_db) as conn:
        row = conn.execute(
            "SELECT current_streak, shields FROM econ_streaks "
            "WHERE guild_id=? AND user_id=?",
            (ECON_GUILD, ECON_USER),
        ).fetchone()
    assert row["current_streak"] == 5  # streak survived
    assert row["shields"] == 0  # shield burned


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_grace_plus_shield_two_day_save(mock_notify, econ_db):
    _enable_econ(econ_db)
    # Two missed days: grace + shield burn together, one combined callout.
    _seed_streak(econ_db, streak=6, last_login_day=_days_ago(3), shields=1)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    embed = mock_notify.await_args.kwargs["embed"]
    saved = next(f for f in embed.fields if "saved" in f.name.lower())
    assert "two missed days" in saved.value.lower()
    assert sum(1 for f in embed.fields if "saved" in f.name.lower()) == 1


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_reset_below_three_omits_reset_field(mock_notify, econ_db):
    _enable_econ(econ_db)
    # A short 2-day streak that breaks (3-day gap) resets — too trivial to
    # call out with its own field, but the daily digest still DMs.
    _seed_streak(econ_db, streak=2, last_login_day=_days_ago(3))
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    assert not any("reset" in f.name.lower() for f in embed.fields)
    with open_db(econ_db) as conn:
        row = conn.execute(
            "SELECT current_streak FROM econ_streaks WHERE guild_id=? AND user_id=?",
            (ECON_GUILD, ECON_USER),
        ).fetchone()
    assert row["current_streak"] == 1  # it did reset, just no dedicated field


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_reset_at_three_plus_dms(mock_notify, econ_db):
    _enable_econ(econ_db)
    # A meaningful streak (>=3) that breaks earns a "streak reset" DM.
    _seed_streak(econ_db, streak=8, last_login_day=_days_ago(4))
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    assert any("reset" in f.name.lower() for f in embed.fields)


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_login_dm_includes_quest_recap(mock_notify, econ_db):
    _enable_econ(econ_db)
    with open_db(econ_db) as conn:
        quest_id = create_quest(
            conn,
            ECON_GUILD,
            title="Say hello",
            description="Chat a bit today.",
            qtype="daily",
            reward=25,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=None,
            created_by=None,
        )
        set_quest_active(conn, ECON_GUILD, quest_id, True)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    mock_notify.assert_awaited_once()
    embed = mock_notify.await_args.kwargs["embed"]
    quest_field = next(f for f in embed.fields if "quest" in f.name.lower())
    assert "Say hello" in quest_field.value


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_qotd_reward_once_per_member(mock_notify, econ_db):
    _enable_econ(econ_db, reward_qotd=10)
    _seed_qotd(econ_db, message_id=999)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message(reply_to=999))
    with open_db(econ_db) as conn:
        rewarded = conn.execute("SELECT COUNT(*) c FROM econ_qotd_rewards").fetchone()["c"]
    assert rewarded == 1
    # A second reply to the same question must not re-reward it.
    await cog._process_economy_message(_econ_message(reply_to=999))
    with open_db(econ_db) as conn:
        rewarded2 = conn.execute("SELECT COUNT(*) c FROM econ_qotd_rewards").fetchone()["c"]
    assert rewarded2 == 1


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_qotd_plain_message_earns_nothing(mock_notify, econ_db):
    """Talking in the channel isn't answering — only a reply pays."""
    _enable_econ(econ_db, reward_qotd=10)
    _seed_qotd(econ_db, message_id=999)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message())
    with open_db(econ_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd_rewards").fetchone()["c"] == 0


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_qotd_stale_question_earns_nothing(mock_notify, econ_db):
    """Yesterday's question is closed — replying to it late can't be farmed."""
    _enable_econ(econ_db, reward_qotd=10)
    _seed_qotd(econ_db, message_id=999, local_day=_days_ago(1))
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(_econ_message(reply_to=999))
    with open_db(econ_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd_rewards").fetchone()["c"] == 0


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_qotd_mod_tag_opens_the_question(mock_notify, econ_db):
    _enable_econ(econ_db, reward_qotd=10, qotd_ping_role_id=77)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(
        _econ_message(
            message_id=555,
            content="<@&77> what's your comfort food?",
            role_mentions=(77,),
            admin=True,
        )
    )
    with open_db(econ_db) as conn:
        row = conn.execute("SELECT message_id, question, local_day FROM econ_qotd").fetchone()
    assert row is not None
    assert row["message_id"] == 555
    assert row["question"] == "what's your comfort food?"
    assert row["local_day"] == _today()
    # …and a member replying to it gets paid.
    await cog._process_economy_message(_econ_message(user_id=ECON_USER + 1, reply_to=555))
    with open_db(econ_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd_rewards").fetchone()["c"] == 1


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_qotd_tag_from_non_mod_opens_nothing(mock_notify, econ_db):
    """The manager gate is the security boundary — anyone can type the tag."""
    _enable_econ(econ_db, reward_qotd=10, qotd_ping_role_id=77)
    cog = _econ_cog(econ_db)
    await cog._process_economy_message(
        _econ_message(message_id=555, content="<@&77> free coins?", role_mentions=(77,))
    )
    with open_db(econ_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd").fetchone()["c"] == 0


@patch("bot_modules.cogs.events_cog.notify_member", new_callable=AsyncMock)
@patch(
    "bot_modules.cogs.events_cog.resolve_accent_color",
    new=AsyncMock(return_value=discord.Color(0x123456)),
)
async def test_econ_qotd_tag_registers_once(mock_notify, econ_db):
    """An edit/retry replaying the same message must not open a second QOTD."""
    _enable_econ(econ_db, reward_qotd=10, qotd_ping_role_id=77)
    cog = _econ_cog(econ_db)
    msg = _econ_message(
        message_id=555, content="<@&77> question?", role_mentions=(77,), admin=True
    )
    await cog._process_economy_message(msg)
    await cog._process_economy_message(msg)
    with open_db(econ_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd").fetchone()["c"] == 1


@patch("bot_modules.cogs.events_cog.handle_level_progress", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.record_member_activity")
@patch("bot_modules.cogs.events_cog.should_track_auto_delete_message", return_value=False)
@patch("bot_modules.cogs.events_cog.award_message_xp", new_callable=AsyncMock)
@patch("bot_modules.cogs.events_cog.enforce_spoiler_requirement", new_callable=AsyncMock)
async def test_econ_hook_failure_never_breaks_on_message(
    mock_spoiler, mock_award, mock_track, mock_activity, mock_level, cog
):
    """A raising economy branch must be swallowed so message/XP processing survives."""
    mock_spoiler.return_value = False
    mock_award.return_value = None
    with patch.object(
        EventsCog,
        "_process_economy_message",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ) as mock_econ:
        # Should not raise despite the economy hook blowing up.
        await cog.on_message(_make_message())
    mock_econ.assert_awaited_once()
    mock_activity.assert_called_once()  # normal processing still completed
