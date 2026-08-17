"""Music playlist logic — link parsing + Spotify matching.

Both upstream OpenMusicBot suites (test_message_parsing, test_matching),
ported with the code they pin down. The matching assertions encode tuned
scoring behavior — a failure here means the constants drifted, not that the
test is stale.
"""

from __future__ import annotations

import pytest

from bot_modules.music_playlist.music_playlist_logic import (
    LinkType,
    SpotifyTrack,
    YouTubeMetadata,
    clean_youtube_title,
    extract_links,
    infer_artist_and_title,
    parse_spotify_playlist_id,
    parse_spotify_url,
    parse_youtube_url,
    select_best_match,
)

# ── Link parsing (upstream: test_message_parsing.py) ──────────────────────────


def test_extract_links_supported_types() -> None:
    message = (
        "track https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=123 "
        "album https://open.spotify.com/album/6akEvsycLGftJxYudPjmqK "
        "playlist https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M "
        "youtube https://www.youtube.com/watch?v=dQw4w9WgXcQ "
        "short https://youtu.be/oHg5SJYRHA0"
    )

    links = extract_links(message)
    assert len(links) == 5
    assert [link.link_type for link in links] == [
        LinkType.SPOTIFY_TRACK,
        LinkType.SPOTIFY_ALBUM,
        LinkType.SPOTIFY_PLAYLIST,
        LinkType.YOUTUBE_VIDEO,
        LinkType.YOUTUBE_VIDEO,
    ]
    assert links[0].item_id == "4uLU6hMCjMI75M1A2tKUQC"
    assert links[1].item_id == "6akEvsycLGftJxYudPjmqK"
    assert links[2].item_id == "37i9dQZF1DXcBWIGoYBM5M"
    assert links[3].item_id == "dQw4w9WgXcQ"
    assert links[4].item_id == "oHg5SJYRHA0"


def test_extract_links_deduplicates_same_id() -> None:
    message = (
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC "
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=dup"
    )
    links = extract_links(message)
    assert len(links) == 1
    assert links[0].item_id == "4uLU6hMCjMI75M1A2tKUQC"


def test_parse_spotify_playlist_id() -> None:
    valid = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc"
    invalid = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
    assert parse_spotify_playlist_id(valid) == "37i9dQZF1DXcBWIGoYBM5M"
    assert parse_spotify_playlist_id(invalid) is None


# Variant rows beyond the upstream suite: each URL shape the parser claims to
# handle, plus the near-misses it must reject. One row per shape, not one test.
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(
            "listen https://m.youtube.com/watch?v=dQw4w9WgXcQ now",
            [(LinkType.YOUTUBE_VIDEO, "dQw4w9WgXcQ")],
            id="mobile-watch",
        ),
        pytest.param(
            "https://www.youtube.com/shorts/oHg5SJYRHA0",
            [(LinkType.YOUTUBE_VIDEO, "oHg5SJYRHA0")],
            id="shorts-path",
        ),
        # The YouTube Music app's own share URL. Missing this host dropped the
        # post silently — no track, no review row, no reaction.
        pytest.param(
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=abc",
            [(LinkType.YOUTUBE_VIDEO, "dQw4w9WgXcQ")],
            id="youtube-music-watch",
        ),
        pytest.param(
            "(https://youtu.be/dQw4w9WgXcQ)",
            [(LinkType.YOUTUBE_VIDEO, "dQw4w9WgXcQ")],
            id="parenthesized-short-link",
        ),
        pytest.param(
            "banger: https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC!",
            [(LinkType.SPOTIFY_TRACK, "4uLU6hMCjMI75M1A2tKUQC")],
            id="trailing-punctuation-stripped",
        ),
        pytest.param(
            "https://www.youtube.com/watch?v=too_short",
            [],
            id="bad-video-id-rejected",
        ),
        pytest.param(
            "https://youtube.com/playlist?list=PL123 https://example.com/track/abc",
            [],
            id="unsupported-paths-and-hosts-rejected",
        ),
        pytest.param(
            "no links here, just vibes",
            [],
            id="plain-text",
        ),
    ],
)
def test_extract_links_variants(
    content: str, expected: list[tuple[LinkType, str]]
) -> None:
    links = extract_links(content)
    assert [(link.link_type, link.item_id) for link in links] == expected


# spotify: URIs never match URL_PATTERN, so they only reach the parser when
# called directly — pin that entry point (and the single-URL rejects) here.
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
            (LinkType.SPOTIFY_TRACK, "4uLU6hMCjMI75M1A2tKUQC"),
            id="track-uri",
        ),
        pytest.param(
            "spotify:album:6akEvsycLGftJxYudPjmqK",
            (LinkType.SPOTIFY_ALBUM, "6akEvsycLGftJxYudPjmqK"),
            id="album-uri",
        ),
        pytest.param("https://open.spotify.com/artist/abc123", None, id="artist-path"),
        pytest.param("https://spotify.example.com/track/abc123", None, id="wrong-host"),
    ],
)
def test_parse_spotify_url_direct(
    url: str, expected: tuple[LinkType, str] | None
) -> None:
    parsed = parse_spotify_url(url)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed.link_type, parsed.item_id) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://youtu.be/not-eleven", id="short-host-bad-id"),
        pytest.param("https://www.youtube.com/watch?t=42", id="watch-without-v"),
    ],
)
def test_parse_youtube_url_rejects(url: str) -> None:
    assert parse_youtube_url(url) is None


# ── Matching (upstream: test_matching.py) ─────────────────────────────────────


def test_clean_youtube_title_removes_noise_tokens() -> None:
    title = "Daft Punk - Harder Better Faster Stronger (Official Video) [HD] Lyrics"
    cleaned = clean_youtube_title(title)
    assert cleaned == "Daft Punk - Harder Better Faster Stronger"


def test_select_best_match_prefers_correct_track() -> None:
    metadata = YouTubeMetadata(
        video_id="abc123def45",
        title="Daft Punk - Harder Better Faster Stronger (Official Video)",
        channel_name="Daft Punk - Topic",
        source_url="https://www.youtube.com/watch?v=abc123def45",
    )
    candidates = [
        SpotifyTrack(
            track_id="correct",
            name="Harder, Better, Faster, Stronger",
            artists=["Daft Punk"],
        ),
        SpotifyTrack(
            track_id="wrong",
            name="Random Song",
            artists=["Another Artist"],
        ),
    ]

    decision = select_best_match(metadata, candidates, threshold=0.74)
    assert decision.best_candidate is not None
    assert decision.best_candidate.track.track_id == "correct"
    assert decision.best_candidate.confidence >= 0.74
    assert decision.is_confident is True


def test_select_best_match_below_threshold_when_unrelated() -> None:
    metadata = YouTubeMetadata(
        video_id="abc123def45",
        title="Unknown Soundtrack Clip",
        channel_name="Movie Clips",
        source_url="https://www.youtube.com/watch?v=abc123def45",
    )
    candidates = [
        SpotifyTrack(track_id="one", name="Different Song", artists=["Band A"]),
        SpotifyTrack(track_id="two", name="Another Track", artists=["Band B"]),
    ]

    decision = select_best_match(metadata, candidates, threshold=0.8)
    assert decision.best_candidate is not None
    assert decision.best_candidate.confidence < 0.8
    assert decision.is_confident is False


# Variant rows: the artist-inference shapes upstream relies on but only
# exercised one of, plus the live-mismatch penalty pulling a match under
# threshold. Same select_best_match surface, different metadata.
@pytest.mark.parametrize(
    ("title", "channel_name", "expected_artist_hint"),
    [
        pytest.param(
            "Harder Better Faster Stronger by Daft Punk",
            None,
            "Daft Punk",
            id="title-by-artist",
        ),
        pytest.param(
            "Harder Better Faster Stronger",
            "Daft Punk - Topic",
            "Daft Punk",
            id="topic-channel-fallback",
        ),
        pytest.param(
            "Harder Better Faster Stronger",
            None,
            None,
            id="no-artist-signal",
        ),
    ],
)
def test_select_best_match_artist_inference_shapes(
    title: str, channel_name: str | None, expected_artist_hint: str | None
) -> None:
    metadata = YouTubeMetadata(
        video_id="abc123def45",
        title=title,
        channel_name=channel_name,
        source_url="https://www.youtube.com/watch?v=abc123def45",
    )
    candidates = [
        SpotifyTrack(
            track_id="correct",
            name="Harder, Better, Faster, Stronger",
            artists=["Daft Punk"],
        ),
    ]

    decision = select_best_match(metadata, candidates, threshold=0.74)
    assert decision.artist_hint == expected_artist_hint
    assert decision.best_candidate is not None
    assert decision.best_candidate.track.track_id == "correct"


def test_live_mismatch_penalized_against_studio_candidate() -> None:
    # "(Live at ...)" survives as a live marker only in the raw candidate
    # comparison — the penalty fires when Spotify's title says live and the
    # cleaned YouTube title doesn't, and the studio cut must outscore it.
    metadata = YouTubeMetadata(
        video_id="abc123def45",
        title="Daft Punk - Harder Better Faster Stronger",
        channel_name="Daft Punk - Topic",
        source_url="https://www.youtube.com/watch?v=abc123def45",
    )
    studio = SpotifyTrack(
        track_id="studio",
        name="Harder, Better, Faster, Stronger",
        artists=["Daft Punk"],
    )
    live = SpotifyTrack(
        track_id="live",
        name="Harder, Better, Faster, Stronger - Live",
        artists=["Daft Punk"],
    )

    decision = select_best_match(metadata, [live, studio], threshold=0.74)
    assert decision.best_candidate is not None
    assert decision.best_candidate.track.track_id == "studio"


def test_live_post_prefers_the_live_cut() -> None:
    """The penalty must read the RAW title, not the cleaned one.

    Regression: ``clean_youtube_title`` strips "live" as noise, so scoring the
    cleaned title made the YouTube side never claim live — and someone posting
    a live recording had the correct live cut penalized while the studio
    version scored clean, the exact inverse of the intent.
    """
    metadata = YouTubeMetadata(
        video_id="abc123def45",
        title="Daft Punk - Harder Better Faster Stronger (Live at Alive 2007)",
        channel_name="Daft Punk - Topic",
        source_url="https://www.youtube.com/watch?v=abc123def45",
    )
    studio = SpotifyTrack(
        track_id="studio",
        name="Harder, Better, Faster, Stronger",
        artists=["Daft Punk"],
    )
    live = SpotifyTrack(
        track_id="live",
        name="Harder, Better, Faster, Stronger - Live",
        artists=["Daft Punk"],
    )

    decision = select_best_match(metadata, [studio, live], threshold=0.74)
    assert decision.best_candidate is not None
    assert decision.best_candidate.track.track_id == "live"


@pytest.mark.parametrize(
    ("title", "expected_hint", "expected_song"),
    [
        # Attribution: lowercase "by" between a song and an artist.
        pytest.param(
            "Yesterday by The Beatles", "The Beatles", "Yesterday",
            id="attribution-lowercase-by",
        ),
        # A title that merely contains the word. Splitting it inferred artist
        # "Me" and song "Stand", poisoning both the search and the score.
        pytest.param("Stand By Me", None, "Stand By Me", id="title-cased-by"),
        pytest.param("stand by me", None, "stand by me", id="lowercase-pronoun"),
    ],
)
def test_by_split_only_fires_on_real_attribution(
    title: str, expected_hint: str | None, expected_song: str
) -> None:
    assert infer_artist_and_title(title, None) == (expected_hint, expected_song)
