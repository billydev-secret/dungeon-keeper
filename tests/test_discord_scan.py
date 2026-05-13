"""Tests for services/discord_scan.py — direct Discord history walker used
by /delete_me to authoritatively find every message a user has posted."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord

from bot_modules.services.discord_scan import (
    collect_messageable_channels,
    find_user_messages,
)


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


def _make_msg(author_id: int, message_id: int) -> MagicMock:
    m = MagicMock()
    m.id = message_id
    m.author = MagicMock()
    m.author.id = author_id
    return m


def _history_returning(messages: list):
    def _factory(**_kwargs):
        async def _gen():
            for msg in messages:
                yield msg
        return _gen()
    return _factory


# ── collect_messageable_channels ────────────────────────────────────────


async def test_collects_text_forum_voice_stage():
    text = _make_channel(discord.TextChannel, 1)
    text.threads = []
    text.archived_threads = MagicMock(return_value=_empty_async_iter())

    forum_thread = _make_channel(discord.Thread, 200)
    forum = _make_channel(discord.ForumChannel, 100)
    forum.threads = [forum_thread]

    async def _archived(**_kwargs):
        if False:
            yield None
    forum.archived_threads = _archived

    voice = _make_channel(discord.VoiceChannel, 300)
    stage = _make_channel(discord.StageChannel, 400)

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = [text]
    guild.forums = [forum]
    guild.voice_channels = [voice]
    guild.stage_channels = [stage]

    me = MagicMock(spec=discord.Member)
    result = await collect_messageable_channels(guild, me)
    ids = {c.id for c in result}
    assert {1, 200, 300, 400}.issubset(ids)
    assert 100 not in ids  # ForumChannel itself excluded


# ── find_user_messages ──────────────────────────────────────────────────


async def test_find_user_messages_filters_by_author():
    """Returns only messages where author.id matches; ignores other users."""
    target_user = 999
    other_user = 111

    channel = _make_channel(discord.TextChannel, 1)
    channel.threads = []
    channel.archived_threads = MagicMock(return_value=_empty_async_iter())
    channel.history = _history_returning([
        _make_msg(target_user, 10),
        _make_msg(other_user, 11),
        _make_msg(target_user, 12),
    ])

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = [channel]
    guild.forums = []
    guild.voice_channels = []
    guild.stage_channels = []
    guild.me = MagicMock(spec=discord.Member)

    rows = await find_user_messages(guild, target_user)
    assert sorted(rows) == [(10, 1), (12, 1)]


async def test_find_user_messages_skips_unreadable_channel():
    """A channel that raises Forbidden during history walk is skipped, not fatal."""
    target_user = 999

    forbidden_channel = _make_channel(discord.TextChannel, 1)
    forbidden_channel.threads = []
    forbidden_channel.archived_threads = MagicMock(return_value=_empty_async_iter())

    async def _raise(**_kwargs):
        raise discord.Forbidden(MagicMock(status=403), "no access")
        yield  # noqa: unreachable
    forbidden_channel.history = _raise

    readable_channel = _make_channel(discord.TextChannel, 2)
    readable_channel.threads = []
    readable_channel.archived_threads = MagicMock(return_value=_empty_async_iter())
    readable_channel.history = _history_returning([_make_msg(target_user, 99)])

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = [forbidden_channel, readable_channel]
    guild.forums = []
    guild.voice_channels = []
    guild.stage_channels = []
    guild.me = MagicMock(spec=discord.Member)

    rows = await find_user_messages(guild, target_user)
    assert rows == [(99, 2)]


async def test_find_user_messages_walks_threads():
    """Forum threads (and their archived members) are scanned, not just text channels."""
    target_user = 999

    forum_thread_active = _make_channel(discord.Thread, 200)
    forum_thread_active.history = _history_returning([_make_msg(target_user, 50)])

    forum_thread_archived = _make_channel(discord.Thread, 201)
    forum_thread_archived.history = _history_returning([_make_msg(target_user, 51)])

    forum = _make_channel(discord.ForumChannel, 100)
    forum.threads = [forum_thread_active]

    async def _archived(**_kwargs):
        yield forum_thread_archived
    forum.archived_threads = _archived

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = []
    guild.forums = [forum]
    guild.voice_channels = []
    guild.stage_channels = []
    guild.me = MagicMock(spec=discord.Member)

    rows = await find_user_messages(guild, target_user)
    assert sorted(rows) == [(50, 200), (51, 201)]


async def test_find_user_messages_progress_callback():
    """on_progress is invoked once per channel with cumulative counts."""
    target_user = 999

    ch1 = _make_channel(discord.TextChannel, 1)
    ch1.threads = []
    ch1.archived_threads = MagicMock(return_value=_empty_async_iter())
    ch1.history = _history_returning([_make_msg(target_user, 10)])

    ch2 = _make_channel(discord.TextChannel, 2)
    ch2.threads = []
    ch2.archived_threads = MagicMock(return_value=_empty_async_iter())
    ch2.history = _history_returning([_make_msg(target_user, 20), _make_msg(target_user, 21)])

    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = [ch1, ch2]
    guild.forums = []
    guild.voice_channels = []
    guild.stage_channels = []
    guild.me = MagicMock(spec=discord.Member)

    calls: list[tuple[int, int, int]] = []

    async def _on_progress(done, total, found):
        calls.append((done, total, found))

    await find_user_messages(guild, target_user, on_progress=_on_progress)
    # Two channels scanned; final call shows 3 messages found across both.
    assert calls == [(1, 2, 1), (2, 2, 3)]
