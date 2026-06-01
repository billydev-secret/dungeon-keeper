"""Tests for the extracted music pure-logic modules.

Covers ``bot_modules/music/logic.py`` (URL/parsing/decision/formatting)
and ``bot_modules/music/embeds.py`` (slash-command embed builders).
Mirrors the pressure-cooker pattern: the cog file stays thin, this
module proves the extracted pieces work without spinning up Discord,
wavelink, or Lavalink.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import discord
import pytest

from bot_modules.music.embeds import build_247_status_embed, build_queue_embed
from bot_modules.music.logic import (
    format_247_status_line,
    format_247_toggle_message,
    format_spotify_summary,
    format_track_summary,
    is_search_url,
    paginate_queue,
    should_idle_disconnect,
    shuffled_autoplay_pool,
    track_summary_from_object,
)


# ── is_search_url ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query, expected",
    [
        ("https://youtube.com/watch?v=abc", True),
        ("http://youtu.be/abc", True),
        ("HTTPS://example.com", False),  # case-sensitive on purpose
        ("ftp://example.com", False),
        ("spotify:track:abc", False),
        ("just a song title", False),
        ("", False),
        ("   https://x.com", False),  # leading whitespace is NOT a URL
    ],
)
def test_is_search_url(query, expected):
    assert is_search_url(query) is expected


# ── paginate_queue ───────────────────────────────────────────────────


def test_paginate_queue_first_page():
    start, end, total_pages, page = paginate_queue(total=25, page=1)
    assert (start, end, total_pages, page) == (0, 10, 3, 1)


def test_paginate_queue_middle_page():
    start, end, total_pages, page = paginate_queue(total=25, page=2)
    assert (start, end, total_pages, page) == (10, 20, 3, 2)


def test_paginate_queue_last_partial_page():
    start, end, total_pages, page = paginate_queue(total=25, page=3)
    # end can land past the data; caller slices, so that's fine
    assert (start, end, total_pages, page) == (20, 30, 3, 3)


def test_paginate_queue_empty_still_reports_one_page():
    start, end, total_pages, page = paginate_queue(total=0, page=1)
    assert (start, end, total_pages, page) == (0, 10, 1, 1)


def test_paginate_queue_normalizes_zero_and_negative_pages_to_one():
    assert paginate_queue(total=25, page=0)[3] == 1
    assert paginate_queue(total=25, page=-5)[3] == 1


def test_paginate_queue_custom_per_page():
    start, end, total_pages, page = paginate_queue(total=50, page=2, per_page=20)
    assert (start, end, total_pages, page) == (20, 40, 3, 2)


def test_paginate_queue_invalid_per_page_falls_back_to_one():
    """A per_page<=0 must not divide by zero -- bumped to 1."""
    start, end, total_pages, page = paginate_queue(total=3, page=1, per_page=0)
    assert (start, end, total_pages, page) == (0, 1, 3, 1)


# ── should_idle_disconnect ───────────────────────────────────────────


def _idle_kwargs(**overrides):
    base = dict(
        humans_present=True,
        playing=True,
        paused=False,
        has_current=True,
        always_on=False,
    )
    base.update(overrides)
    return base


def test_should_idle_disconnect_never_drops_247_channel():
    """24/7 trumps every other condition."""
    assert should_idle_disconnect(**_idle_kwargs(always_on=True)) is False
    # Even alone + nothing playing -- still no disconnect.
    assert (
        should_idle_disconnect(
            **_idle_kwargs(
                always_on=True, humans_present=False, playing=False, has_current=False
            )
        )
        is False
    )


def test_should_idle_disconnect_keeps_session_when_humans_are_listening():
    assert should_idle_disconnect(**_idle_kwargs()) is False
    # Paused but humans present is also "still listening".
    assert (
        should_idle_disconnect(**_idle_kwargs(playing=False, paused=True))
        is False
    )


def test_should_idle_disconnect_drops_when_alone():
    assert should_idle_disconnect(**_idle_kwargs(humans_present=False)) is True


def test_should_idle_disconnect_drops_when_nothing_playing():
    """Humans present but the queue is dead -- we drop instead of camping."""
    assert (
        should_idle_disconnect(
            **_idle_kwargs(playing=False, paused=False, has_current=False)
        )
        is True
    )


def test_should_idle_disconnect_drops_when_humans_present_but_no_current():
    """``has_current=False`` defeats the humans-present hold."""
    assert (
        should_idle_disconnect(**_idle_kwargs(has_current=False))
        is True
    )


# ── format_track_summary ─────────────────────────────────────────────


def test_format_track_summary_with_uri_uses_masked_link():
    out = format_track_summary("Song", "Artist", "https://x/abc")
    assert out == "[Song -- Artist](<https://x/abc>)"


def test_format_track_summary_without_uri():
    assert format_track_summary("Song", "Artist", None) == "Song -- Artist"


def test_format_track_summary_falls_back_to_unknown_only_when_title_is_none():
    """Only ``None`` triggers the Unknown fallback -- empty string passes
    through to match the original cog's ``getattr(..., "Unknown")``
    semantics, which never collapsed ``""`` into ``"Unknown"``."""
    assert format_track_summary(None, "Artist", None) == "Unknown -- Artist"
    assert format_track_summary("", "Artist", None) == " -- Artist"


def test_format_track_summary_uses_fallback_author_when_track_author_missing():
    out = format_track_summary("Song", None, None, fallback_author="Spotify Artist")
    assert out == "Song -- Spotify Artist"


def test_format_track_summary_question_mark_when_no_author_anywhere():
    assert format_track_summary("Song", None, None) == "Song -- ?"


def test_track_summary_from_object_pulls_fields_off_namespace():
    track = SimpleNamespace(title="T", author="A", uri="https://x")
    assert track_summary_from_object(track) == "[T -- A](<https://x>)"


def test_track_summary_from_object_uses_fallback_author():
    """Wavelink tracks coming from Spotify sometimes have no ``author``."""
    track = SimpleNamespace(title="T", author=None, uri=None)
    assert track_summary_from_object(track, fallback_author="Spotify A") == "T -- Spotify A"


def test_track_summary_from_object_handles_object_with_no_attrs():
    """Missing attributes shouldn't raise -- fallbacks all the way down."""
    track = object()
    assert track_summary_from_object(track) == "Unknown -- ?"


# ── format_spotify_summary ───────────────────────────────────────────


def test_format_spotify_summary_track_added():
    out = format_spotify_summary(
        kind="track",
        name=None,
        added=1,
        truncated=False,
        first_summary="[T -- A](<u>)",
        page_size=1,
    )
    assert out == "Queued: [T -- A](<u>)"


def test_format_spotify_summary_track_no_match():
    out = format_spotify_summary(
        kind="track",
        name=None,
        added=0,
        truncated=False,
        first_summary="",
        page_size=0,
    )
    assert out == "No match found."


@pytest.mark.parametrize(
    "added, expected_phrase",
    [
        (1, "**1** top track by"),
        (3, "**3** top tracks by"),
    ],
)
def test_format_spotify_summary_artist_pluralisation(added, expected_phrase):
    out = format_spotify_summary(
        kind="artist",
        name="Some Artist",
        added=added,
        truncated=False,
        first_summary="",
        page_size=10,
    )
    assert expected_phrase in out
    assert "Some Artist" in out


def test_format_spotify_summary_artist_unknown_name():
    out = format_spotify_summary(
        kind="artist", name=None, added=5, truncated=False, first_summary="", page_size=5
    )
    assert "Unknown" in out


def test_format_spotify_summary_playlist_basic():
    out = format_spotify_summary(
        kind="playlist",
        name="My Mix",
        added=12,
        truncated=False,
        first_summary="",
        page_size=12,
    )
    assert "**12** tracks from playlist **My Mix**" in out
    assert "truncated" not in out


def test_format_spotify_summary_album_basic():
    out = format_spotify_summary(
        kind="album",
        name="Album",
        added=4,
        truncated=False,
        first_summary="",
        page_size=4,
    )
    assert "**4** tracks from album **Album**" in out


def test_format_spotify_summary_truncation_suffix_mentions_page_size():
    out = format_spotify_summary(
        kind="playlist",
        name="Huge",
        added=500,
        truncated=True,
        first_summary="",
        page_size=500,
    )
    assert "truncated to first 500" in out


def test_format_spotify_summary_singular_when_added_is_one():
    out = format_spotify_summary(
        kind="playlist",
        name="X",
        added=1,
        truncated=False,
        first_summary="",
        page_size=1,
    )
    assert "**1** track from" in out
    assert "**1** tracks from" not in out


# ── format_247_toggle_message ────────────────────────────────────────


def test_format_247_toggle_message_disable_path_ignores_extras():
    out = format_247_toggle_message(
        enabled=False,
        channel_mention="<#5>",
        cleared_mentions=["<#1>"],   # ignored when disabling
        autoplay_saved=True,
        join_error="boom",
    )
    assert out == "24/7 disabled for <#5>."


def test_format_247_toggle_message_enable_minimal():
    out = format_247_toggle_message(enabled=True, channel_mention="<#5>")
    assert out == "24/7 enabled for <#5>."


def test_format_247_toggle_message_enable_with_cleared_channels():
    out = format_247_toggle_message(
        enabled=True,
        channel_mention="<#5>",
        cleared_mentions=["<#1>", "<#2>"],
    )
    assert "Disabled previous 24/7 channel(s): <#1>, <#2>." in out


def test_format_247_toggle_message_enable_with_autoplay():
    out = format_247_toggle_message(
        enabled=True, channel_mention="<#5>", autoplay_saved=True
    )
    assert "Autoplay playlist saved." in out


def test_format_247_toggle_message_enable_with_join_error():
    out = format_247_toggle_message(
        enabled=True, channel_mention="<#5>", join_error="no perms"
    )
    assert "(Couldn't join right now: no perms)" in out


def test_format_247_toggle_message_enable_all_branches_combined():
    out = format_247_toggle_message(
        enabled=True,
        channel_mention="<#5>",
        cleared_mentions=["<#1>"],
        autoplay_saved=True,
        join_error="boom",
    )
    lines = out.split("\n")
    assert lines[0] == "24/7 enabled for <#5>."
    assert "Disabled previous 24/7 channel(s): <#1>." in lines[1]
    assert lines[2] == "Autoplay playlist saved."
    assert lines[3] == "(Couldn't join right now: boom)"


# ── format_247_status_line ───────────────────────────────────────────


def test_format_247_status_line_plain():
    assert format_247_status_line("<#5>", False) == "• <#5>"


def test_format_247_status_line_with_autoplay():
    assert format_247_status_line("<#5>", True) == "• <#5> (autoplay)"


# ── shuffled_autoplay_pool ───────────────────────────────────────────


def test_shuffled_autoplay_pool_seeded_rng_is_deterministic():
    items = list(range(10))
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    assert shuffled_autoplay_pool(items, cap=5, rng=rng_a) == shuffled_autoplay_pool(
        items, cap=5, rng=rng_b
    )


def test_shuffled_autoplay_pool_trims_to_cap():
    out = shuffled_autoplay_pool(range(100), cap=5, rng=random.Random(0))
    assert len(out) == 5


def test_shuffled_autoplay_pool_no_cap_returns_full_shuffle():
    """The cog's autoplay loop relies on this: full pool returned so it
    can stop at N successful adds (search misses don't count)."""
    out = shuffled_autoplay_pool([1, 2, 3, 4, 5], rng=random.Random(0))
    assert sorted(out) == [1, 2, 3, 4, 5]


def test_shuffled_autoplay_pool_cap_exceeds_pool_returns_full_pool():
    out = shuffled_autoplay_pool([1, 2, 3], cap=50, rng=random.Random(0))
    assert sorted(out) == [1, 2, 3]


def test_shuffled_autoplay_pool_handles_empty():
    assert shuffled_autoplay_pool([], cap=5, rng=random.Random(0)) == []


def test_shuffled_autoplay_pool_negative_cap_returns_empty():
    assert shuffled_autoplay_pool([1, 2, 3], cap=-1, rng=random.Random(0)) == []


def test_shuffled_autoplay_pool_uses_module_random_when_no_rng_supplied():
    """No rng => uses global random; ensure it doesn't raise and returns right size."""
    out = shuffled_autoplay_pool(range(20), cap=5)
    assert len(out) == 5
    # All values came from the input pool
    assert set(out).issubset(set(range(20)))


# ── build_queue_embed ────────────────────────────────────────────────


def test_build_queue_embed_with_current_and_items():
    embed = build_queue_embed(
        current_summary="Now: X",
        item_summaries=["Track 1", "Track 2"],
        start_index=0,
        total_in_queue=2,
        page=1,
        total_pages=1,
        loop_mode_value="off",
    )
    assert embed.title == "Music queue"
    fields = {f.name: f.value or "" for f in embed.fields}
    assert fields["Now playing"] == "Now: X"
    assert "Up next (2 total)" in fields
    assert " 1." in fields["Up next (2 total)"]
    assert " 2." in fields["Up next (2 total)"]
    assert embed.footer.text == "Page 1/1 · loop: off"


def test_build_queue_embed_without_current_skips_now_playing_field():
    embed = build_queue_embed(
        current_summary=None,
        item_summaries=["A"],
        start_index=0,
        total_in_queue=1,
        page=1,
        total_pages=1,
        loop_mode_value="off",
    )
    field_names = [f.name for f in embed.fields]
    assert "Now playing" not in field_names


def test_build_queue_embed_empty_queue_says_empty():
    embed = build_queue_embed(
        current_summary="X",
        item_summaries=[],
        start_index=0,
        total_in_queue=0,
        page=1,
        total_pages=1,
        loop_mode_value="off",
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Up next"] == "(empty)"


def test_build_queue_embed_numbers_continue_across_pages():
    """Page 2 -- start_index=10 -- should number 11, 12, 13..."""
    embed = build_queue_embed(
        current_summary=None,
        item_summaries=["a", "b", "c"],
        start_index=10,
        total_in_queue=13,
        page=2,
        total_pages=2,
        loop_mode_value="queue",
    )
    fields = {f.name: f.value or "" for f in embed.fields}
    body = fields["Up next (13 total)"]
    assert "11." in body
    assert "12." in body
    assert "13." in body


def test_build_queue_embed_footer_reflects_loop_mode():
    embed = build_queue_embed(
        current_summary=None,
        item_summaries=[],
        start_index=0,
        total_in_queue=0,
        page=2,
        total_pages=3,
        loop_mode_value="track",
    )
    assert embed.footer.text == "Page 2/3 · loop: track"


# ── build_247_status_embed ───────────────────────────────────────────


def test_build_247_status_embed_basic():
    embed = build_247_status_embed(["• <#1>", "• <#2> (autoplay)"])
    assert isinstance(embed, discord.Embed)
    assert embed.title == "24/7 channels"
    assert "<#1>" in (embed.description or "")
    assert "(autoplay)" in (embed.description or "")


def test_build_247_status_embed_empty_falls_back_to_placeholder():
    embed = build_247_status_embed([])
    assert embed.description == "(none)"
