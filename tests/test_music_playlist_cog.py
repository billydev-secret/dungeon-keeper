"""Wiring tests for the music playlist cog.

The pipeline itself (gates, resolution, dedupe, writes, the remove-on-delete
dial) is covered in tests/test_music_playlist_service.py — re-proving it here
through Discord mocks would be the cog-test bloat CLAUDE.md warns against.
These assert only the glue: the listener's cheap gates, the summary→reaction
mapping, the raw-delete hand-off, and that the extension is registered.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import discord
import pytest

from bot_modules.cogs.music_playlist_cog import (
    REACTION_ADDED,
    REACTION_DUPLICATE,
    REACTION_QUEUED,
    MusicPlaylistCog,
)
from bot_modules.music_playlist.embeds import (
    build_my_review_embed,
    build_window_embed,
)
from bot_modules.music_playlist.music_playlist_service import (
    DeleteResult,
    MusicPlaylistSettings,
    ProcessingSummary,
)

SPOTIFY_LINK = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def _make_cog() -> MusicPlaylistCog:
    bot = MagicMock()
    ctx = MagicMock()
    ctx.db_path = Path(":memory:")
    bot.ctx = ctx
    cog = MusicPlaylistCog(bot)
    cog.service = AsyncMock()
    return cog


def _message(*, content: str = SPOTIFY_LINK, bot_author: bool = False):
    message = MagicMock()
    message.guild = MagicMock()
    message.guild.id = 1
    message.channel.id = 5
    message.id = 99
    message.author.bot = bot_author
    message.author.id = 10
    message.content = content
    message.add_reaction = AsyncMock()
    return message


# ── on_message gates ─────────────────────────────────────────────────────


async def test_on_message_ignores_bots():
    cog = _make_cog()
    await cog.on_message(_message(bot_author=True))
    cog.service.process_message.assert_not_awaited()


async def test_on_message_ignores_dms():
    cog = _make_cog()
    message = _message()
    message.guild = None
    await cog.on_message(message)
    cog.service.process_message.assert_not_awaited()


async def test_on_message_ignores_linkless_messages():
    cog = _make_cog()
    await cog.on_message(_message(content="no links here, just vibes"))
    cog.service.process_message.assert_not_awaited()


# ── summary → reactions ──────────────────────────────────────────────────


async def test_on_message_hands_link_message_to_service_and_reacts():
    cog = _make_cog()
    cog.service.process_message.return_value = ProcessingSummary(
        links_found=3,
        added_track_ids=["a"],
        duplicate_count=1,
        unmatched_ids=[7],
    )
    message = _message()

    await cog.on_message(message)

    cog.service.process_message.assert_awaited_once_with(
        1, 5, 99, SPOTIFY_LINK, 10
    )
    assert message.add_reaction.await_args_list == [
        call(REACTION_ADDED),
        call(REACTION_DUPLICATE),
        call(REACTION_QUEUED),
    ]


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        pytest.param(ProcessingSummary(skipped=True), [], id="skipped"),
        pytest.param(
            ProcessingSummary(links_found=1, added_track_ids=["a"]),
            [call(REACTION_ADDED)],
            id="added-only",
        ),
        pytest.param(
            ProcessingSummary(links_found=1, duplicate_count=1),
            [call(REACTION_DUPLICATE)],
            id="duplicate-only",
        ),
        pytest.param(
            ProcessingSummary(links_found=1, unmatched_ids=[3]),
            [call(REACTION_QUEUED)],
            id="queued-only",
        ),
    ],
)
async def test_reaction_mapping(summary, expected):
    cog = _make_cog()
    cog.service.process_message.return_value = summary
    message = _message()

    await cog.on_message(message)

    assert message.add_reaction.await_args_list == expected


async def test_failed_reaction_does_not_break_the_listener():
    cog = _make_cog()
    cog.service.process_message.return_value = ProcessingSummary(
        links_found=1, added_track_ids=["a"]
    )
    message = _message()
    message.add_reaction.side_effect = discord.HTTPException(
        MagicMock(status=403), "no perms"
    )

    await cog.on_message(message)  # must not raise


# ── raw delete hand-off ──────────────────────────────────────────────────


async def test_raw_delete_calls_service():
    cog = _make_cog()
    cog.service.handle_message_deleted.return_value = DeleteResult(
        removed_track_ids=[], errors=[]
    )
    payload = MagicMock()
    payload.guild_id = 1
    payload.message_id = 99

    await cog.on_raw_message_delete(payload)

    cog.service.handle_message_deleted.assert_awaited_once_with(1, 99)


async def test_raw_delete_ignores_dms():
    cog = _make_cog()
    payload = MagicMock()
    payload.guild_id = None

    await cog.on_raw_message_delete(payload)

    cog.service.handle_message_deleted.assert_not_awaited()


# ── dashboard maintenance hooks ──────────────────────────────────────────
# The pipeline behind each processed message is the service's (tested there);
# these cover the cog's sweep glue: gates, history order, and the counts the
# panel renders.


def _settings(**overrides):
    values = {"enabled": True, "channel_id": 5, "playlist_id": "pl"}
    values.update(overrides)
    return MusicPlaylistSettings(**values)


class _FakeChannel:
    """Just enough of TextChannel: history(limit=) → async iterator."""

    def __init__(self, messages):
        self._messages = messages  # newest first, as Discord returns them
        self.history_limit: int | None = None

    def history(self, *, limit):
        self.history_limit = limit
        messages = self._messages[:limit]

        async def gen():
            for message in messages:
                yield message

        return gen()


async def test_rescan_requires_configuration():
    cog = _make_cog()
    cog.service.load_settings.return_value = _settings(channel_id=0)
    result = await cog.rescan_channel(1)
    assert result["ok"] is False
    cog.service.process_message.assert_not_awaited()


async def test_rescan_reports_missing_channel():
    cog = _make_cog()
    cog.service.load_settings.return_value = _settings()
    cog.bot.get_channel.return_value = None
    result = await cog.rescan_channel(1)
    assert result["ok"] is False and "channel" in result["error"]


async def test_rescan_sweeps_oldest_first_and_counts():
    cog = _make_cog()
    cog.service.load_settings.return_value = _settings()
    newest = _message()
    newest.id = 103
    ledgered = _message()
    ledgered.id = 102
    oldest = _message()
    oldest.id = 101
    # Newest first (Discord order), plus a bot post and a linkless post the
    # sweep must skip without touching the service.
    channel = _FakeChannel([
        newest,
        _message(bot_author=True),
        ledgered,
        _message(content="no links, just vibes"),
        oldest,
    ])
    cog.bot.get_channel.return_value = channel
    cog.service.process_message.side_effect = [
        ProcessingSummary(links_found=1, added_track_ids=["a"]),
        ProcessingSummary(skipped=True),  # already in the ledger
        ProcessingSummary(links_found=1, duplicate_count=1, unmatched_ids=[7]),
    ]

    result = await cog.rescan_channel(1)

    # Oldest first, so the window ends up holding the newest posts.
    assert [
        c.args[2] for c in cog.service.process_message.await_args_list
    ] == [101, 102, 103]
    assert result == {
        "ok": True, "scanned": 3, "added": 1,
        "duplicates": 1, "queued": 1, "errors": 0,
    }
    # A sweep never reacts on old messages.
    newest.add_reaction.assert_not_awaited()


async def test_rescan_counts_a_failing_message_and_continues():
    cog = _make_cog()
    cog.service.load_settings.return_value = _settings()
    first = _message()
    first.id = 101
    second = _message()
    second.id = 102
    channel = _FakeChannel([second, first])
    cog.bot.get_channel.return_value = channel
    cog.service.process_message.side_effect = [
        RuntimeError("boom"),
        ProcessingSummary(links_found=1, added_track_ids=["a"]),
    ]
    result = await cog.rescan_channel(1)
    assert result["errors"] == 1 and result["added"] == 1


async def test_reconcile_delegates_to_service():
    cog = _make_cog()
    cog.service.reconcile.return_value = {"ok": True, "added": 2, "removed": 0}
    result = await cog.reconcile_playlist(1)
    # Unconfirmed by default — the service withholds a bulk delete.
    cog.service.reconcile.assert_awaited_once_with(1, confirm_removals=False)
    assert result == {"ok": True, "added": 2, "removed": 0}


async def test_reconcile_passes_the_confirmation_through():
    cog = _make_cog()
    cog.service.reconcile.return_value = {"ok": True, "added": 0, "removed": 9}
    await cog.reconcile_playlist(1, confirm_removals=True)
    cog.service.reconcile.assert_awaited_once_with(1, confirm_removals=True)


# ── registration ─────────────────────────────────────────────────────────


def test_extension_is_registered():
    main_py = (
        Path(__file__).resolve().parents[1]
        / "src" / "dungeonkeeper" / "__main__.py"
    )
    assert '"bot_modules.cogs.music_playlist_cog"' in main_py.read_text(
        encoding="utf-8"
    )


# ── embed smoke (content, not accent — the accent contract table owns
#    passthrough/fallback) ────────────────────────────────────────────────

ACCENT = discord.Color(0x123456)


def test_window_embed_lists_rows_and_escapes_markdown():
    rows = [
        {"title": "Song **One**", "artist": "_Artist_", "added_by": 10},
        {"title": "Song Two", "artist": "Band", "added_by": 11},
    ]
    embed = build_window_embed(rows, window_size=30, color=ACCENT)
    assert embed.description is not None
    assert "Song \\*\\*One\\*\\*" in embed.description
    assert "<@11>" in embed.description
    assert embed.footer.text is not None and "2/30" in embed.footer.text


def test_window_embed_empty_state():
    embed = build_window_embed([], window_size=30, color=ACCENT)
    assert embed.description == (
        "Nothing in the playlist yet — post a song link to start it."
    )


def test_review_embed_shows_candidate_or_no_match():
    items = [
        {
            "extracted_title": "Cool Song (Official Video)",
            "source_url": "https://youtu.be/x",
            "candidate_name": "Cool Song",
            "candidate_artist": "Cool Band",
        },
        {
            "extracted_title": None,
            "source_url": "https://youtu.be/y",
            "candidate_name": None,
            "candidate_artist": None,
        },
    ]
    embed = build_my_review_embed(items, color=ACCENT)
    assert embed.description is not None
    assert "best guess: Cool Song — Cool Band" in embed.description
    assert "no match found" in embed.description
