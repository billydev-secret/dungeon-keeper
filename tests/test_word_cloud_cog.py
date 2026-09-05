"""Wiring tests for the ``/wordcloud`` cog.

Deliberately thin — the tokenising and counting are exercised in
``test_word_cloud_logic``. What is here is the glue that logic cannot reach:
the read-permission gate, which is the *whole* authorisation story for this
command, and the dial clamping that stands between a bad config row and an
empty or runaway render.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from bot_modules.cogs.word_cloud_cog import (
    DEFAULT_CAP,
    MAX_CAP,
    WordCloudCog,
    _readable_channel_ids,
)

GUILD = 7


class _Perms:
    def __init__(self, read_messages: bool, read_message_history: bool) -> None:
        self.read_messages = read_messages
        self.read_message_history = read_message_history


class _Channel:
    def __init__(self, cid: int, perms: _Perms) -> None:
        self.id = cid
        self._perms = perms

    def permissions_for(self, _member):
        return self._perms


def _guild(text_channels=(), threads=()):
    return SimpleNamespace(
        id=GUILD, text_channels=list(text_channels), threads=list(threads)
    )


# --------------------------------------------------------------------------
# The read-permission gate
# --------------------------------------------------------------------------


def test_readable_channels_includes_what_the_member_can_read():
    guild = _guild(text_channels=[_Channel(1, _Perms(True, True))])
    assert _readable_channel_ids(guild, object()) == [1]


def test_readable_channels_excludes_channels_the_member_cannot_see():
    guild = _guild(
        text_channels=[
            _Channel(1, _Perms(True, True)),
            _Channel(2, _Perms(False, True)),
        ]
    )
    assert _readable_channel_ids(guild, object()) == [1]


def test_readable_channels_requires_history_not_just_view():
    """Seeing a channel exists is not permission to read what was said in it."""
    guild = _guild(text_channels=[_Channel(3, _Perms(True, False))])
    assert _readable_channel_ids(guild, object()) == []


def test_readable_channels_includes_threads():
    """The archive stores a thread's own id, so skipping them drops whole
    conversations."""
    guild = _guild(
        text_channels=[_Channel(1, _Perms(True, True))],
        threads=[_Channel(9, _Perms(True, True))],
    )
    assert _readable_channel_ids(guild, object()) == [1, 9]


def test_readable_channels_filters_threads_too():
    guild = _guild(threads=[_Channel(9, _Perms(False, True))])
    assert _readable_channel_ids(guild, object()) == []


def test_readable_channels_on_an_empty_guild():
    assert _readable_channel_ids(_guild(), object()) == []


class _Thread(discord.Thread):
    """A real ``discord.Thread`` for the isinstance branch, without a client."""

    def __init__(self, cid: int, perms, *, private: bool, parent_cached: bool = True):
        self.id = cid
        self._perms = perms
        self._private = private
        self._parent_cached = parent_cached

    def permissions_for(self, _member):
        if not self._parent_cached:
            raise discord.ClientException("Parent channel not found")
        return self._perms

    def is_private(self) -> bool:
        return self._private


def _thread_perms(*, manage_threads: bool):
    perms = _Perms(True, True)
    perms.manage_threads = manage_threads
    return perms


def test_readable_channels_excludes_a_private_thread_the_member_isnt_in():
    """``Thread.permissions_for`` only inherits the parent's overwrites, so a
    private thread the moderator was never added to still reads as allowed.

    Without the explicit check, `everywhere` would cloud the words of a room
    the invoker cannot open — the one thing this command promises it can't do.
    """
    guild = _guild(threads=[_Thread(9, _thread_perms(manage_threads=False), private=True)])
    assert _readable_channel_ids(guild, object()) == []


def test_readable_channels_keeps_a_private_thread_for_manage_threads():
    """Manage Threads is Discord's own way into a private thread."""
    guild = _guild(threads=[_Thread(9, _thread_perms(manage_threads=True), private=True)])
    assert _readable_channel_ids(guild, object()) == [9]


def test_readable_channels_keeps_public_threads_without_manage_threads():
    guild = _guild(threads=[_Thread(9, _thread_perms(manage_threads=False), private=False)])
    assert _readable_channel_ids(guild, object()) == [9]


def test_readable_channels_skips_a_thread_whose_parent_isnt_cached():
    """One uncached parent raises ClientException; it must not sink the lot."""
    guild = _guild(
        text_channels=[_Channel(1, _Perms(True, True))],
        threads=[
            _Thread(
                9, _thread_perms(manage_threads=True), private=False, parent_cached=False
            )
        ],
    )
    assert _readable_channel_ids(guild, object()) == [1]


# --------------------------------------------------------------------------
# Dial reading
# --------------------------------------------------------------------------


def _cog(stored: dict[str, str], *, retains: bool = True) -> WordCloudCog:
    conn = sqlite3.connect(":memory:")
    # get_config_value reads row["value"], so the row factory is load-bearing.
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    for key, value in stored.items():
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, key, value),
        )

    @contextmanager
    def open_db():
        yield conn

    ctx = SimpleNamespace(
        open_db=open_db,
        guild_config=lambda _gid: SimpleNamespace(retains_content=retains),
    )
    return WordCloudCog(SimpleNamespace(ctx=ctx))


def test_read_dials_returns_stored_values():
    cap, preset, retains = _cog(
        {"word_cloud_message_cap": "500", "word_cloud_default_preset": "neon"}
    )._read_dials(GUILD)
    assert (cap, preset, retains) == (500, "neon", True)


def test_read_dials_defaults_when_unset():
    cap, preset, _ = _cog({})._read_dials(GUILD)
    assert cap == DEFAULT_CAP
    assert preset == "midnight"


@pytest.mark.parametrize("stored", ["0", "-5"])
def test_read_dials_treats_a_useless_cap_as_unset(stored):
    """A cap of 0 must mean "no cap set", never "render nothing"."""
    cap, _, _ = _cog({"word_cloud_message_cap": stored})._read_dials(GUILD)
    assert cap == DEFAULT_CAP


def test_read_dials_clamps_an_oversized_cap():
    cap, _, _ = _cog({"word_cloud_message_cap": "999999"})._read_dials(GUILD)
    assert cap == MAX_CAP


def test_read_dials_survives_a_non_numeric_cap():
    cap, _, _ = _cog({"word_cloud_message_cap": "lots"})._read_dials(GUILD)
    assert cap == DEFAULT_CAP


def test_read_dials_reports_a_content_free_guild():
    _, _, retains = _cog({}, retains=False)._read_dials(GUILD)
    assert retains is False


# --------------------------------------------------------------------------
# Reply copy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=30), "30 minutes"),
        (timedelta(hours=1), "1 hour"),
        (timedelta(hours=24), "24 hours"),
        (timedelta(days=7), "7 days"),
        (timedelta(days=1), "24 hours"),
    ],
)
def test_span_label_reads_naturally(span, expected):
    assert WordCloudCog._span_label(span) == expected


def test_empty_message_names_the_missing_archive():
    """"Quiet week" and "this server keeps no text" must not read alike."""
    msg = WordCloudCog._empty_message(False, None, "#general")
    assert "doesn't keep message text" in msg


def test_empty_message_for_a_quiet_window_names_the_scope():
    msg = WordCloudCog._empty_message(True, None, "#general")
    assert "#general" in msg
    assert "doesn't keep message text" not in msg


def test_empty_message_names_the_member_by_display_name():
    """An embed never carries a raw <@id> — see docs/embed_style_guide.md."""
    member = SimpleNamespace(display_name="Robin")
    msg = WordCloudCog._empty_message(True, member, "#general")
    assert "Robin" in msg
    assert "<@" not in msg
