"""Tests for the extracted music pure-logic modules.

Covers ``bot_modules/music/logic.py`` (URL/parsing/decision/formatting)
and ``bot_modules/music/embeds.py`` (slash-command embed builders).
Mirrors the pressure-cooker pattern: the cog file stays thin, this
module proves the extracted pieces work without spinning up Discord,
wavelink, or Lavalink.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot_modules.music.embeds import build_queue_embed
from bot_modules.music.logic import (
    describe_track_failure,
    format_spotify_summary,
    format_track_summary,
    is_search_url,
    paginate_queue,
    should_idle_disconnect,
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
    )
    base.update(overrides)
    return base


def test_should_idle_disconnect_drops_an_idle_empty_channel():
    """No channel is exempt any more. Until 2026-07-28 an always_on flag made
    24/7 channels trump every other condition; the feature is gone, so an empty
    channel with nothing playing always disconnects."""
    assert (
        should_idle_disconnect(
            **_idle_kwargs(humans_present=False, playing=False, has_current=False)
        )
        is True
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
    assert embed.title == "🎶 Music queue"
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


# ── describe_track_failure ───────────────────────────────────────────

# A realistic AllClientsFailedException cause: the live one measured
# 6,621 chars, which is what used to blow Discord's 2000-char limit.
_SME_CAUSE = (
    "com.sedmelluq.discord.lavaplayer.tools.FriendlyException: "
    "All clients failed to load the item.\n"
    "Client [ANDROID_VR] blocked due to the claimed content by SME.\n"
    "Client [WEB] blocked due to the claimed content by SME.\n"
    "Client [WEB_EMBEDDED_PLAYER] Video player configuration error\n"
    + "\tat dev.lavalink.youtube.SomeFrame.load(SomeFrame.java:123)\n" * 120
)


def test_failure_message_rights_block_is_plain_and_sendable():
    """Regression: the raw exception was ~6.6k chars, the send failed, and
    the user saw nothing. The message must fit Discord and say why."""
    msg = describe_track_failure(
        "Song Title", message="All clients failed to load the item.", cause=_SME_CAUSE
    )
    assert len(msg) < 2000
    assert "Song Title" in msg
    assert "rights holder" in msg
    assert "SomeFrame" not in msg  # no stack-trace leakage


@pytest.mark.parametrize(
    "message, cause, expected_reason",
    [
        ("x", "Client [WEB] blocked due to the claimed content by SME.", "rights holder"),
        ("This video requires payment / copyright claim", None, "rights holder"),
        ("Sign in to confirm your age", None, "age-restricted"),
        (None, "This video is age-restricted", "age-restricted"),
        ("This is a private video", None, "private"),
        ("This video is unavailable", None, "unavailable"),
        ("Track not available in your country", None, "region"),
    ],
)
def test_failure_message_maps_known_reasons(message, cause, expected_reason):
    msg = describe_track_failure("T", message=message, cause=cause)
    assert expected_reason in msg


def test_failure_message_unknown_reason_keeps_first_message_line():
    msg = describe_track_failure(
        "T", message="Something exploded.\nat java.base/whatever", cause=None
    )
    assert msg == "⚠️ Couldn't play **T** — Something exploded. Skipping."


def test_failure_message_no_details_at_all():
    msg = describe_track_failure(None)
    assert msg == "⚠️ Couldn't play that track — playback failed. Skipping."


def test_failure_message_clips_absurd_title_and_detail():
    msg = describe_track_failure("t" * 500, message="m" * 500)
    assert len(msg) < 400
    assert "…" in msg


