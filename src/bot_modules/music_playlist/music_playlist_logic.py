"""Link parsing and Spotify matching for the music playlist channel.

Ported from OpenMusicBot's ``message_parsing.py`` + ``matching.py``
(docs/plans/music-playlist-cog.md). Two halves, both pure:

* **Parsing** — pull every supported music link out of message content.
  Spotify track/album/playlist URLs and ``spotify:`` URIs, plus YouTube
  watch/Shorts/youtu.be links. Album and playlist links are still
  *classified* (``LinkType`` keeps all four kinds) — whether to expand or
  skip them is the service's call, behind the ``expand_albums`` dial.
* **Matching** — turn a noisy YouTube title into a best-guess Spotify track:
  strip filler ("official video", "[HD]", "lyrics"), infer the artist from
  ``Artist - Title`` / ``Title by Artist`` / the ``… - Topic`` channel
  convention, then score candidates.

The scoring constants are tuned behavior carried over verbatim — the
0.72/0.28 title/artist blend, the +0.06 exact-substring bonus, and the
live/remaster/acoustic mismatch penalties (0.08/0.07/0.07). Don't nudge
them without re-running the ported suites against real channel traffic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from urllib.parse import parse_qs, urlparse

# ── Models ────────────────────────────────────────────────────────────────────


class LinkType(str, Enum):
    SPOTIFY_TRACK = "spotify_track"
    SPOTIFY_ALBUM = "spotify_album"
    SPOTIFY_PLAYLIST = "spotify_playlist"
    YOUTUBE_VIDEO = "youtube_video"


@dataclass(frozen=True, slots=True)
class ParsedLink:
    raw_url: str
    link_type: LinkType
    item_id: str


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    track_id: str
    name: str
    artists: list[str]
    url: str | None = None


@dataclass(frozen=True, slots=True)
class YouTubeMetadata:
    video_id: str
    title: str
    channel_name: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    track: SpotifyTrack
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class MatchDecision:
    cleaned_title: str
    artist_hint: str | None
    threshold: float
    best_candidate: MatchCandidate | None = None

    @property
    def is_confident(self) -> bool:
        return bool(
            self.best_candidate and self.best_candidate.confidence >= self.threshold
        )


# ── Link parsing ──────────────────────────────────────────────────────────────

URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
SPOTIFY_PATH_PATTERN = re.compile(
    r"^/(track|album|playlist)/([A-Za-z0-9]+)$", re.IGNORECASE
)
SPOTIFY_URI_PATTERN = re.compile(
    r"^spotify:(track|album|playlist):([A-Za-z0-9]+)$", re.IGNORECASE
)
YOUTUBE_SHORT_PATTERN = re.compile(r"^/([A-Za-z0-9_-]{11})$")
YOUTUBE_SHORTS_PATTERN = re.compile(r"^/shorts/([A-Za-z0-9_-]{11})$")

_TRAILING_PUNCTUATION = ".,!?:;)]}>\"'"


def _clean_token(token: str) -> str:
    return token.rstrip(_TRAILING_PUNCTUATION)


def _spotify_kind(kind: str) -> LinkType:
    if kind.lower() == "track":
        return LinkType.SPOTIFY_TRACK
    if kind.lower() == "album":
        return LinkType.SPOTIFY_ALBUM
    return LinkType.SPOTIFY_PLAYLIST


def parse_spotify_url(url: str) -> ParsedLink | None:
    """Parse Spotify track/album/playlist URLs and ``spotify:`` URIs."""

    raw = _clean_token(url.strip())
    uri_match = SPOTIFY_URI_PATTERN.match(raw)
    if uri_match:
        kind, item_id = uri_match.groups()
        return ParsedLink(raw_url=raw, link_type=_spotify_kind(kind), item_id=item_id)

    parsed = urlparse(raw)
    if parsed.netloc.lower() not in {"open.spotify.com"}:
        return None

    match = SPOTIFY_PATH_PATTERN.match(parsed.path)
    if not match:
        return None

    kind, item_id = match.groups()
    return ParsedLink(raw_url=raw, link_type=_spotify_kind(kind), item_id=item_id)


def parse_spotify_playlist_id(url: str) -> str | None:
    parsed = parse_spotify_url(url)
    if not parsed or parsed.link_type is not LinkType.SPOTIFY_PLAYLIST:
        return None
    return parsed.item_id


def parse_youtube_url(url: str) -> ParsedLink | None:
    """Parse standard YouTube, Shorts, and youtu.be links."""

    raw = _clean_token(url.strip())
    parsed = urlparse(raw)
    host = parsed.netloc.lower()

    if host in {"youtu.be", "www.youtu.be"}:
        match = YOUTUBE_SHORT_PATTERN.match(parsed.path)
        if match:
            return ParsedLink(
                raw_url=raw,
                link_type=LinkType.YOUTUBE_VIDEO,
                item_id=match.group(1),
            )
        return None

    # music.youtube.com is the share URL the YouTube Music app produces, which
    # in a music channel is the *likely* form, not an exotic one. Omitting it
    # meant those posts fell out of the pipeline entirely — no track, no review
    # queue row, and no reaction, so the member saw silence rather than a miss.
    if host not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        return None

    if parsed.path == "/watch":
        query = parse_qs(parsed.query)
        video_ids = query.get("v")
        if not video_ids:
            return None
        video_id = video_ids[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return ParsedLink(
                raw_url=raw, link_type=LinkType.YOUTUBE_VIDEO, item_id=video_id
            )
        return None

    shorts_match = YOUTUBE_SHORTS_PATTERN.match(parsed.path)
    if shorts_match:
        return ParsedLink(
            raw_url=raw,
            link_type=LinkType.YOUTUBE_VIDEO,
            item_id=shorts_match.group(1),
        )

    return None


def extract_links(content: str) -> list[ParsedLink]:
    """Extract all supported links from message content, first-seen order.

    Duplicate ids collapse to one link; album/playlist links come back
    classified, not filtered — skipping them is service policy, not parsing.
    """

    extracted: list[ParsedLink] = []
    seen: set[tuple[LinkType, str]] = set()

    for token in URL_PATTERN.findall(content):
        parsed = parse_spotify_url(token) or parse_youtube_url(token)
        if not parsed:
            continue
        key = (parsed.link_type, parsed.item_id)
        if key in seen:
            continue
        seen.add(key)
        extracted.append(parsed)

    return extracted


# ── Title cleaning + matching ─────────────────────────────────────────────────

NOISE_PATTERNS = (
    r"\bofficial\s+video\b",
    r"\bofficial\s+music\s+video\b",
    r"\bofficial\s+audio\b",
    r"\blyrics?\b",
    r"\baudio\b",
    r"\bvisualizer\b",
    r"\b4k\b",
    r"\b8k\b",
    r"\bhd\b",
    r"\blive\b",
    r"\bremaster(?:ed)?\b",
    r"\bsped\s*up\b",
    r"\bslowed(?:\s+and\s+reverb)?\b",
    r"\bexplicit\b",
    r"\bclean\b",
    r"\bvideo\s+clip\b",
)

NOISE_REGEX = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)
BRACKETED_REGEX = re.compile(r"\[[^\]]+\]|\([^)]+\)")
WHITESPACE_REGEX = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    lowered = text.lower()
    collapsed = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return WHITESPACE_REGEX.sub(" ", collapsed).strip()


def clean_youtube_title(title: str) -> str:
    cleaned = BRACKETED_REGEX.sub(" ", title)
    cleaned = NOISE_REGEX.sub(" ", cleaned)
    cleaned = cleaned.replace("|", " ").replace("_", " ")
    cleaned = WHITESPACE_REGEX.sub(" ", cleaned).strip(" -")
    return cleaned or title.strip()


# Objects of the preposition "by", never an artist name. Catches the lowercase
# spellings that survive the title-case rule ("stand by me").
_NOT_AN_ARTIST = frozenset(
    {"me", "you", "us", "him", "her", "them", "myself", "yourself"}
)


def infer_artist_and_title(
    cleaned_title: str, channel_name: str | None
) -> tuple[str | None, str]:
    """Try to infer artist + song title from YouTube metadata."""

    if " - " in cleaned_title:
        artist_part, title_part = cleaned_title.split(" - ", maxsplit=1)
        artist_hint = normalize_text(artist_part)
        if artist_hint:
            return artist_part.strip(), title_part.strip()

    # Attribution ("Yesterday by The Beatles"), not a title that merely
    # contains the word. Matched case-sensitively on a lowercase "by": titles
    # using it as a preposition are conventionally title-cased ("Stand By Me"),
    # and treating those as attribution inferred artist "Me" / song "Stand",
    # which poisoned both the search queries and the artist half of the score
    # and pushed common songs into the review queue. An all-caps title falls
    # through to channel-name inference instead, which is the safer miss.
    if " by " in cleaned_title:
        title_part, artist_part = cleaned_title.split(" by ", maxsplit=1)
        title_part, artist_part = title_part.strip(), artist_part.strip()
        if (
            artist_part
            and title_part
            and artist_part.lower() not in _NOT_AN_ARTIST
        ):
            return artist_part, title_part

    if channel_name:
        artist_hint = channel_name.replace(" - Topic", "").strip()
        if artist_hint:
            return artist_hint, cleaned_title.strip()

    return None, cleaned_title.strip()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _mismatch_penalty(youtube_title: str, spotify_title: str) -> float:
    """Penalize when exactly one side claims a live/remaster/acoustic cut.

    ``youtube_title`` must be the **raw** title. ``clean_youtube_title``
    strips "live" and "remaster" as noise, so scoring the cleaned title here
    made ``yt_has`` permanently False for those two markers and inverted the
    intent: someone posting "Song (Live at Wembley)" had the genuine live cut
    penalized while the studio version scored clean.
    """

    yt_norm = normalize_text(youtube_title)
    sp_norm = normalize_text(spotify_title)
    penalty = 0.0

    for marker, cost in (("live", 0.08), ("remaster", 0.07), ("acoustic", 0.07)):
        yt_has = marker in yt_norm
        sp_has = marker in sp_norm
        if yt_has != sp_has:
            penalty += cost

    return penalty


def score_candidate(
    song_title: str,
    artist_hint: str | None,
    candidate: SpotifyTrack,
    *,
    raw_title: str | None = None,
) -> tuple[float, str]:
    """Confidence score between inferred YouTube metadata and a Spotify track.

    Tuned constants — keep in lockstep with the ported tests: 0.72/0.28
    title/artist blend, +0.06 substring bonus, mismatch penalties above.

    ``raw_title`` is the pre-cleaning title, used only for the live/remaster
    penalty (see :func:`_mismatch_penalty`). It defaults to ``song_title`` so
    direct callers keep working, but ``select_best_match`` always passes the
    real one.
    """

    normalized_song = normalize_text(song_title)
    candidate_title = normalize_text(candidate.name)

    title_score = _similarity(normalized_song, candidate_title)
    artist_score = 0.5
    reason = "title-only"

    if artist_hint:
        normalized_artist = normalize_text(artist_hint)
        best_artist_score = 0.0
        for artist_name in candidate.artists:
            best_artist_score = max(
                best_artist_score,
                _similarity(normalized_artist, normalize_text(artist_name)),
            )
        artist_score = best_artist_score
        reason = "title+artist"

    score = (0.72 * title_score) + (0.28 * artist_score)

    if normalized_song and normalized_song in candidate_title:
        score += 0.06
        reason = f"{reason}+substring"

    penalty = _mismatch_penalty(
        song_title if raw_title is None else raw_title, candidate.name
    )
    score -= penalty
    score = max(0.0, min(1.0, score))
    return score, reason


def select_best_match(
    metadata: YouTubeMetadata, candidates: Iterable[SpotifyTrack], threshold: float
) -> MatchDecision:
    cleaned_title = clean_youtube_title(metadata.title)
    artist_hint, inferred_song_title = infer_artist_and_title(
        cleaned_title, metadata.channel_name
    )

    best: MatchCandidate | None = None
    for track in candidates:
        confidence, reason = score_candidate(
            inferred_song_title, artist_hint, track,
            raw_title=metadata.title,
        )
        if best is None or confidence > best.confidence:
            best = MatchCandidate(track=track, confidence=confidence, reason=reason)

    return MatchDecision(
        cleaned_title=cleaned_title,
        artist_hint=artist_hint,
        threshold=threshold,
        best_candidate=best,
    )
