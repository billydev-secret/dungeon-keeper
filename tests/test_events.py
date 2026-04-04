"""Tests for Discord event handlers."""
from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import app_commands

from handlers.events import register_events


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EventCapture:
    """Captures event callbacks registered via @bot.event and @bot.tree.error."""

    def __init__(self):
        self.events: dict[str, Any] = {}
        self.error_handler = None
        bot = MagicMock()
        bot.event = self._capture_event
        bot.tree.error = self._capture_error
        self.bot = bot

    def _capture_event(self, fn):
        self.events[fn.__name__] = fn
        return fn

    def _capture_error(self, fn):
        self.error_handler = fn
        return fn

    def get(self, name: str):
        return self.events[name]


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


def _make_message(
    *,
    is_bot: bool = False,
    guild: Any = MagicMock(),
    channel_id: int = 10,
    author_id: int = 50,
    message_id: int = 1000,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    author = MagicMock(spec=discord.Member)
    author.bot = is_bot
    author.id = author_id
    msg.author = author
    msg.guild = guild
    msg.id = message_id
    channel = MagicMock()
    channel.id = channel_id
    msg.channel = channel
    msg.created_at = MagicMock()
    msg.created_at.timestamp = MagicMock(return_value=1_000_000.0)
    return msg


# ---------------------------------------------------------------------------
# on_message tests
# ---------------------------------------------------------------------------


class OnMessageTests(unittest.TestCase):
    def setUp(self):
        cap = _EventCapture()
        self.ctx = _make_ctx()
        register_events(cap.bot, self.ctx)
        self.on_message = cap.get("on_message")

    @patch("handlers.events.record_member_activity")
    @patch("handlers.events.award_message_xp", new_callable=AsyncMock)
    @patch("handlers.events.enforce_spoiler_requirement", new_callable=AsyncMock)
    def test_bot_message_ignored(self, mock_spoiler, mock_award, mock_activity):
        mock_spoiler.return_value = False
        msg = _make_message(is_bot=True)
        _run(self.on_message(msg))
        mock_activity.assert_not_called()
        mock_award.assert_not_called()

    @patch("handlers.events.record_member_activity")
    @patch("handlers.events.award_message_xp", new_callable=AsyncMock)
    @patch("handlers.events.enforce_spoiler_requirement", new_callable=AsyncMock)
    def test_dm_message_ignored(self, mock_spoiler, mock_award, mock_activity):
        mock_spoiler.return_value = False
        msg = _make_message(guild=None)
        _run(self.on_message(msg))
        mock_activity.assert_not_called()
        mock_award.assert_not_called()

    @patch("handlers.events.handle_level_progress", new_callable=AsyncMock)
    @patch("handlers.events.record_member_activity")
    @patch("handlers.events.auto_delete_rule_exists", return_value=False)
    @patch("handlers.events.award_message_xp", new_callable=AsyncMock)
    @patch("handlers.events.enforce_spoiler_requirement", new_callable=AsyncMock)
    def test_spoiler_violation_stops_processing(
        self, mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level
    ):
        mock_spoiler.return_value = True
        msg = _make_message()
        _run(self.on_message(msg))
        mock_activity.assert_not_called()
        mock_award.assert_not_called()

    @patch("handlers.events.handle_level_progress", new_callable=AsyncMock)
    @patch("handlers.events.record_member_activity")
    @patch("handlers.events.auto_delete_rule_exists", return_value=False)
    @patch("handlers.events.award_message_xp", new_callable=AsyncMock)
    @patch("handlers.events.enforce_spoiler_requirement", new_callable=AsyncMock)
    def test_normal_message_records_activity(
        self, mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level
    ):
        mock_spoiler.return_value = False
        mock_award.return_value = None
        msg = _make_message()
        _run(self.on_message(msg))
        mock_activity.assert_called_once()

    @patch("handlers.events.handle_level_progress", new_callable=AsyncMock)
    @patch("handlers.events.track_auto_delete_message")
    @patch("handlers.events.record_member_activity")
    @patch("handlers.events.auto_delete_rule_exists", return_value=True)
    @patch("handlers.events.award_message_xp", new_callable=AsyncMock)
    @patch("handlers.events.enforce_spoiler_requirement", new_callable=AsyncMock)
    def test_message_tracked_when_rule_exists(
        self, mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_track, mock_level
    ):
        mock_spoiler.return_value = False
        mock_award.return_value = None
        msg = _make_message(channel_id=10, message_id=999)
        _run(self.on_message(msg))
        mock_track.assert_called_once()
        args = mock_track.call_args[0]
        self.assertEqual(args[2], 10)   # channel_id
        self.assertEqual(args[3], 999)  # message_id

    @patch("handlers.events.handle_level_progress", new_callable=AsyncMock)
    @patch("handlers.events.record_member_activity")
    @patch("handlers.events.auto_delete_rule_exists", return_value=False)
    @patch("handlers.events.award_message_xp", new_callable=AsyncMock)
    @patch("handlers.events.enforce_spoiler_requirement", new_callable=AsyncMock)
    def test_xp_award_triggers_level_progress(
        self, mock_spoiler, mock_award, mock_rule_exists, mock_activity, mock_level
    ):
        mock_spoiler.return_value = False
        award_result = MagicMock()
        mock_award.return_value = award_result
        msg = _make_message()
        # author must be discord.Member for level progress to fire
        msg.author = MagicMock(spec=discord.Member)
        msg.author.bot = False
        msg.author.id = 50
        _run(self.on_message(msg))
        mock_level.assert_awaited_once()


# ---------------------------------------------------------------------------
# on_raw_message_delete tests
# ---------------------------------------------------------------------------


class OnRawMessageDeleteTests(unittest.TestCase):
    def setUp(self):
        cap = _EventCapture()
        self.ctx = _make_ctx()
        register_events(cap.bot, self.ctx)
        self.on_delete = cap.get("on_raw_message_delete")

    @patch("services.auto_delete_service.remove_tracked_auto_delete_message")
    def test_no_guild_id_ignored(self, mock_remove):
        payload = MagicMock(spec=discord.RawMessageDeleteEvent)
        payload.guild_id = None
        _run(self.on_delete(payload))
        mock_remove.assert_not_called()

    @patch("services.auto_delete_service.remove_tracked_auto_delete_message")
    def test_with_guild_id_removes_message(self, mock_remove):
        payload = MagicMock(spec=discord.RawMessageDeleteEvent)
        payload.guild_id = 1
        payload.channel_id = 10
        payload.message_id = 999
        _run(self.on_delete(payload))
        mock_remove.assert_called_once_with(self.ctx.db_path, 1, 10, 999)


# ---------------------------------------------------------------------------
# on_raw_bulk_message_delete tests
# ---------------------------------------------------------------------------


class OnRawBulkMessageDeleteTests(unittest.TestCase):
    def setUp(self):
        cap = _EventCapture()
        self.ctx = _make_ctx()
        register_events(cap.bot, self.ctx)
        self.on_bulk_delete = cap.get("on_raw_bulk_message_delete")

    @patch("services.auto_delete_service.remove_tracked_auto_delete_messages")
    def test_no_guild_id_ignored(self, mock_remove):
        payload = MagicMock(spec=discord.RawBulkMessageDeleteEvent)
        payload.guild_id = None
        _run(self.on_bulk_delete(payload))
        mock_remove.assert_not_called()

    @patch("services.auto_delete_service.remove_tracked_auto_delete_messages")
    def test_with_guild_id_removes_messages(self, mock_remove):
        payload = MagicMock(spec=discord.RawBulkMessageDeleteEvent)
        payload.guild_id = 1
        payload.channel_id = 10
        payload.message_ids = {100, 101, 102}
        _run(self.on_bulk_delete(payload))
        mock_remove.assert_called_once_with(self.ctx.db_path, 1, 10, {100, 101, 102})


# ---------------------------------------------------------------------------
# on_raw_reaction_add tests
# ---------------------------------------------------------------------------


class OnRawReactionAddTests(unittest.TestCase):
    def setUp(self):
        cap = _EventCapture()
        self.ctx = _make_ctx()
        register_events(cap.bot, self.ctx)
        self.on_reaction = cap.get("on_raw_reaction_add")

    @patch("handlers.events.handle_level_progress", new_callable=AsyncMock)
    @patch("handlers.events.award_image_reaction_xp", new_callable=AsyncMock)
    def test_no_award_no_level_progress(self, mock_award, mock_level):
        mock_award.return_value = None
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        _run(self.on_reaction(payload))
        mock_level.assert_not_awaited()

    @patch("handlers.events.handle_level_progress", new_callable=AsyncMock)
    @patch("handlers.events.award_image_reaction_xp", new_callable=AsyncMock)
    def test_award_triggers_level_progress(self, mock_award, mock_level):
        member = MagicMock(spec=discord.Member)
        award_result = MagicMock()
        mock_award.return_value = (member, award_result)
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        _run(self.on_reaction(payload))
        mock_level.assert_awaited_once()
        args, kwargs = mock_level.call_args
        self.assertEqual(args[0], member)
        self.assertEqual(args[1], award_result)
        self.assertEqual(args[2], "image_reaction")


# ---------------------------------------------------------------------------
# on_app_command_error tests
# ---------------------------------------------------------------------------


class OnAppCommandErrorTests(unittest.TestCase):
    def setUp(self):
        cap = _EventCapture()
        self.ctx = _make_ctx()
        register_events(cap.bot, self.ctx)
        self.on_error = cap.error_handler

    def _make_interaction(self):
        ix = MagicMock(spec=discord.Interaction)
        ix.response.is_done = MagicMock(return_value=False)
        ix.response.send_message = AsyncMock()
        ix.guild_id = 1
        ix.user = MagicMock()
        ix.user.id = 100
        return ix

    def test_command_not_found_sends_ephemeral(self):
        ix = self._make_interaction()
        error = app_commands.CommandNotFound("unknown_cmd", [])
        _run(self.on_error(ix, error))
        ix.response.send_message.assert_awaited_once()
        self.assertTrue(ix.response.send_message.call_args[1]["ephemeral"])
        self.assertIn("out of date", ix.response.send_message.call_args[0][0].lower())

    def test_command_not_found_skipped_if_response_done(self):
        ix = self._make_interaction()
        ix.response.is_done.return_value = True
        error = app_commands.CommandNotFound("cmd", [])
        _run(self.on_error(ix, error))
        ix.response.send_message.assert_not_awaited()

    def test_generic_error_sends_failure_message(self):
        ix = self._make_interaction()
        error = app_commands.AppCommandError("something broke")
        _run(self.on_error(ix, error))
        ix.response.send_message.assert_awaited_once()
        self.assertTrue(ix.response.send_message.call_args[1]["ephemeral"])
        self.assertIn("failed", ix.response.send_message.call_args[0][0].lower())


if __name__ == "__main__":
    unittest.main()
