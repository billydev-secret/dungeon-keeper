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
    failure_reason,
    format_spotify_summary,
    format_track_summary,
    is_search_url,
    paginate_queue,
    pick_substitute,
    should_advance_on_track_end,
    should_idle_disconnect,
    substitute_queries,
    substitute_query,
    substitution_note,
    track_summary_from_object,
)
from bot_modules.services.music_queue import GuildQueue
from bot_modules.core.branding import SECTION_SPACER


def _unspaced(value: str | None) -> str:
    """A field value without the trailing spacer ``apply_section_spacing`` adds.

    Every field but the last carries ``SECTION_SPACER`` for breathing room
    (docs/embed_style_guide.md § Section spacing). These tests assert content,
    not spacing, so they compare against the value with it removed.
    """
    text = value or ""
    return text[: -len(SECTION_SPACER)] if text.endswith(SECTION_SPACER) else text



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
    assert embed.title == "🎶 Music Queue"
    fields = {f.name: _unspaced(f.value) for f in embed.fields}
    assert fields["Now Playing"] == "Now: X"
    assert "Up next (2 total)" in fields
    assert " 1." in fields["Up next (2 total)"]
    assert " 2." in fields["Up next (2 total)"]
    assert embed.footer.text == "Page 1/1 • loop: off"


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
    assert "Now Playing" not in field_names


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
    fields = {f.name: _unspaced(f.value) for f in embed.fields}
    assert fields["Up Next"] == "(empty)"


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
    fields = {f.name: _unspaced(f.value) for f in embed.fields}
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
    assert embed.footer.text == "Page 2/3 • loop: track"


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


def test_failure_reason_unknown_is_none():
    assert failure_reason(message="mysterious", cause="also mysterious") is None


# ── should_advance_on_track_end ──────────────────────────────────────


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("finished", True),
        # 'replaced' fires on /skip and on substitute playback; advancing on
        # it dropped the middle track of a 3-deep queue on every /skip.
        ("replaced", False),
        # 'loadFailed' follows every TrackExceptionEvent; the exception
        # handler owns recovery, advancing here clobbers its substitute.
        ("loadFailed", False),
        ("stopped", False),
        ("cleanup", False),
        (None, False),
    ],
)
def test_should_advance_on_track_end(reason, expected):
    assert should_advance_on_track_end(reason) is expected


# ── substitute_query ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title, author, expected",
    [
        ("Song Title (Official Video)", "Artist", "Song Title Artist"),
        ("Song Title [HD] (Lyrics)", "ArtistVEVO", "Song Title Artist"),
        ("Song Title", "Artist - Topic", "Song Title Artist"),
        # Author already inside the title: don't repeat it.
        ("Artist - Song Title", "Artist", "Artist - Song Title"),
        ("Song Title", None, "Song Title"),
        (None, "Artist", "Artist"),
    ],
)
def test_substitute_query(title, author, expected):
    assert substitute_query(title, author) == expected


@pytest.mark.parametrize(
    "title, author, expected",
    [
        # Real case from 2026-07-30: the uploader channel is unrelated to
        # the song, so the channel-augmented query found nothing and the
        # bare title was the query that rescued the track.
        (
            "GOT TO GIVE IT UP - MARVIN GAYE",
            "O.E.U. Studios",
            [
                "GOT TO GIVE IT UP - MARVIN GAYE O.E.U. Studios",
                "GOT TO GIVE IT UP - MARVIN GAYE",
            ],
        ),
        # Author already in the title: both variants collapse to one query.
        ("Artist - Song Title", "Artist", ["Artist - Song Title"]),
        # Bracket-stripping applies to the title-only variant too.
        (
            "Song Title (Official Video)",
            "Artist",
            ["Song Title Artist", "Song Title"],
        ),
        (None, None, []),
    ],
)
def test_substitute_queries(title, author, expected):
    assert substitute_queries(title, author) == expected


# ── pick_substitute ──────────────────────────────────────────────────


def _cand(title, author="Artist", length=222_000, identifier="cand-id"):
    return SimpleNamespace(
        title=title, author=author, length=length, identifier=identifier
    )


_ORIGINAL = dict(
    original_title="Song Title (Official Video)",
    original_author="Artist",
    original_length_ms=222_000,
)


def test_pick_substitute_accepts_same_track_different_upload():
    good = _cand("Song Title (Lyric Video)", length=220_000)
    assert (
        pick_substitute([good], **_ORIGINAL)
        is good
    )


def test_pick_substitute_skips_bad_candidates_to_reach_a_good_one():
    hour_mix = _cand("Song Title 1 hour", length=3_600_000)
    clip = _cand("Song Title", length=44_000)
    cover = _cand("Song Title (cover)", length=221_000)
    good = _cand("Artist - Song Title", length=225_000)
    picked = pick_substitute([hour_mix, clip, cover, good], **_ORIGINAL)
    assert picked is good


@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param(_cand("Song Title (sped up)", length=180_000), id="sped-up"),
        pytest.param(_cand("Song Title slowed + reverb", length=260_000), id="slowed"),
        pytest.param(_cand("Song Title nightcore", length=222_000), id="nightcore"),
        pytest.param(_cand("Song Title LIVE at Arena", length=230_000), id="live"),
        pytest.param(_cand("Song Title instrumental", length=222_000), id="instrumental"),
        pytest.param(_cand("Different Song Entirely", length=222_000), id="unrelated"),
    ],
)
def test_pick_substitute_rejects_variants_and_mismatches(candidate):
    assert pick_substitute([candidate], **_ORIGINAL) is None


def test_pick_substitute_allows_variant_terms_the_original_had():
    # The member queued a remix on purpose; a remix substitute is correct.
    remix = _cand("Song Title (Remix)", length=222_000)
    assert (
        pick_substitute(
            [remix],
            original_title="Song Title (Remix)",
            original_author="Artist",
            original_length_ms=222_000,
        )
        is remix
    )


def test_pick_substitute_excludes_the_failed_upload_itself():
    same = _cand("Song Title", identifier="failed-id")
    assert (
        pick_substitute([same], **_ORIGINAL, exclude_identifiers={"failed-id"})
        is None
    )


def test_pick_substitute_skips_duration_check_when_lengths_unknown():
    unknown = _cand("Song Title", length=0)
    assert pick_substitute([unknown], **_ORIGINAL) is unknown


def test_pick_substitute_empty_candidates_is_none():
    assert pick_substitute([], **_ORIGINAL) is None


# ── substitution_note ────────────────────────────────────────────────


def test_substitution_note_soundcloud_with_reason():
    note = substitution_note(
        "Song Title",
        "Song Title",
        "Artist",
        source="soundcloud",
        reason="it's blocked by the rights holder on YouTube",
    )
    assert note == (
        "⚠️ Couldn't play **Song Title** — it's blocked by the rights holder "
        "on YouTube. Playing the closest match from SoundCloud instead: "
        "**Song Title — Artist**."
    )
    assert len(note) < 2000


def test_substitution_note_youtube_alternate_upload_no_reason():
    note = substitution_note("Song Title", "Song Title (Audio)", "Artist2", source="youtube")
    assert note == (
        "⚠️ Couldn't play **Song Title**. Playing another upload instead: "
        "**Song Title (Audio) — Artist2**."
    )


# ── GuildQueue.adopt_requester ───────────────────────────────────────


def test_adopt_requester_carries_requester_to_substitute():
    queue = GuildQueue(guild_id=1)
    failed = SimpleNamespace(identifier="failed-id")
    substitute = SimpleNamespace(identifier="sub-id")
    queue.add(failed, requester_id=42)
    queue.adopt_requester(failed, substitute)
    assert queue.requester_for(substitute) == 42


def test_adopt_requester_noop_when_original_had_no_requester():
    queue = GuildQueue(guild_id=1)
    failed = SimpleNamespace(identifier="failed-id")
    substitute = SimpleNamespace(identifier="sub-id")
    queue.adopt_requester(failed, substitute)
    assert queue.requester_for(substitute) is None


