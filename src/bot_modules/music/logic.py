"""Pure decision/format helpers for the music cog.

Everything here takes plain Python inputs and returns plain Python
values -- no Discord, no wavelink, no I/O. The cog assembles its state
(player, queue, settings) and hands the bits these functions need.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, TypeVar

T = TypeVar("T")


# ── URL / query classification ───────────────────────────────────────


def is_search_url(query: str) -> bool:
    """Return True when ``query`` is a URL that wavelink should treat verbatim.

    Plain-text queries get the YouTube source prefix prepended by wavelink;
    URLs must be passed through untouched. Match the cog's behavior:
    only ``http://`` / ``https://`` count, anything else is a search.
    """
    return query.startswith(("http://", "https://"))


# ── Pagination math ──────────────────────────────────────────────────


def paginate_queue(
    total: int, page: int, per_page: int = 10
) -> tuple[int, int, int, int]:
    """Compute slice bounds + page count for the queue embed.

    Returns ``(start, end, total_pages, normalized_page)``. ``page`` is
    clamped to at least 1; ``end`` is past-the-last so it can feed a
    Python slice directly. An empty queue still reports one page so the
    embed footer reads ``Page 1/1`` rather than ``1/0``.
    """
    per_page = max(1, per_page)
    normalized = max(1, page)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (normalized - 1) * per_page
    end = start + per_page
    return start, end, total_pages, normalized


# ── Idle-disconnect gate ─────────────────────────────────────────────


def should_idle_disconnect(
    *,
    humans_present: bool,
    playing: bool,
    paused: bool,
    has_current: bool,
) -> bool:
    """Decide whether the idle watcher should drop the voice connection.

    The cog used to inline this across two helpers (``_can_idle_disconnect``
    and the gate inside ``_idle_disconnect``). Hoisting it keeps the
    matrix testable: if humans are still listening to something that's
    playing or paused, we wait; otherwise (empty channel, or nothing
    playing) we drop.

    Had an ``always_on`` escape hatch until 2026-07-28, when the 24/7
    feature was removed — no channel is exempt now.
    """
    if humans_present and (playing or paused) and has_current:
        return False
    return True


# ── Track summary string ─────────────────────────────────────────────


def format_track_summary(
    title: str | None,
    author: str | None,
    uri: str | None,
    *,
    fallback_author: str | None = None,
) -> str:
    """Render the single-line track summary used in /play and /queue.

    Mirrors ``MusicCog._track_summary`` but pulls the fields out so it's
    callable from tests with plain strings. Missing title (``None``)
    falls back to ``"Unknown"`` -- matches the cog's
    ``getattr(track, "title", "Unknown")`` semantics, which preserve an
    explicit empty string. Missing author falls back to the Spotify
    primary artist if supplied else ``"?"``. URL-aware: wraps in masked-
    link syntax with ``<...>`` brackets to suppress Discord's URL
    preview when ``uri`` is present.
    """
    safe_title = "Unknown" if title is None else title
    safe_author = author or fallback_author or "?"
    if uri:
        return f"[{safe_title} -- {safe_author}](<{uri}>)"
    return f"{safe_title} -- {safe_author}"


# ── Spotify enqueue summary ──────────────────────────────────────────


def format_spotify_summary(
    *,
    kind: str,
    name: str | None,
    added: int,
    truncated: bool,
    first_summary: str,
    page_size: int,
) -> str:
    """Build the user-facing summary after a Spotify URL has been queued.

    Branches mirror ``MusicCog._enqueue_spotify``:

    * ``track`` -- single-line "Queued: ..." or "No match found."
    * ``artist`` -- "Queued **N** top track(s) by **Artist**."
    * ``playlist`` / ``album`` -- "Queued **N** track(s) from
      playlist/album **Name**." with optional truncation suffix.

    ``page_size`` is the cap used when paging the playlist; surfaced in
    the truncation suffix so the user knows where the cut happened.
    """
    if kind == "track":
        return f"Queued: {first_summary}" if added else "No match found."

    plural = "s" if added != 1 else ""
    label = name or "Unknown"

    if kind == "artist":
        return f"Queued **{added}** top track{plural} by **{label}**."

    kind_label = "playlist" if kind == "playlist" else "album"
    warn = f"\n(Playlist truncated to first {page_size} tracks.)" if truncated else ""
    return (
        f"Queued **{added}** track{plural} from {kind_label} **{label}**."
        f"{warn}"
    )


# ── Track-failure messaging ──────────────────────────────────────────

# Substring of the Lavalink exception (message or cause) -> plain-language
# reason. Matched lowercase, first hit wins; ordered most-specific first.
_FAILURE_REASONS: tuple[tuple[str, str], ...] = (
    ("blocked due to the claimed content", "it's blocked by the rights holder on YouTube"),
    ("copyright", "it's blocked by the rights holder on YouTube"),
    ("sign in to confirm your age", "it's age-restricted on YouTube"),
    ("age-restricted", "it's age-restricted on YouTube"),
    ("private video", "the video is private"),
    ("video is unavailable", "the video is unavailable"),
    ("not available in your country", "it's not available in the bot's region"),
)

_MAX_TITLE = 120
_MAX_DETAIL = 140


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def failure_reason(
    *, message: str | None = None, cause: str | None = None
) -> str | None:
    """Map a Lavalink exception to a plain-language reason, or None."""
    haystack = f"{message or ''}\n{cause or ''}".lower()
    for needle, reason in _FAILURE_REASONS:
        if needle in haystack:
            return reason
    return None


def describe_track_failure(
    title: str | None,
    *,
    message: str | None = None,
    cause: str | None = None,
) -> str:
    """One short, Discord-safe line explaining why a track failed.

    Lavalink's exception payload is ``{message, severity, cause}`` where
    ``cause`` is a multi-KB Java stack trace -- sending it raw blows the
    2000-char message limit and the notify silently drops (the original
    "track vanishes with no explanation" bug). This maps the known
    failure modes to plain language and hard-caps everything else, so
    the returned string is always sendable.
    """
    label = f"**{_clip(title, _MAX_TITLE)}**" if title else "that track"
    reason = failure_reason(message=message, cause=cause)
    if reason is not None:
        return f"⚠️ Couldn't play {label} — {reason}. Skipping."
    # Unknown failure: keep the first line of Lavalink's message as a hint.
    detail = (message or "").strip().splitlines()[0] if message else ""
    if detail:
        detail = _clip(detail.rstrip("."), _MAX_DETAIL)
        return f"⚠️ Couldn't play {label} — {detail}. Skipping."
    return f"⚠️ Couldn't play {label} — playback failed. Skipping."


# ── Track-end advance gate ───────────────────────────────────────────


def should_advance_on_track_end(reason: str | None) -> bool:
    """Only a natural finish advances the queue from ``track_end``.

    Lavalink ends *every* track with a reason and wavelink dispatches the
    event unconditionally: ``finished`` (played out), ``replaced`` (we
    deliberately started another track -- /skip, a substitute),
    ``loadFailed`` (the track-exception handler owns recovery and
    advancing), ``stopped`` (/stop or disconnect), ``cleanup`` (player
    destroyed). Advancing on anything but ``finished`` double-advances:
    a /skip with three queued tracks dropped the middle one, and a load
    failure would clobber the substitute the exception handler started.
    """
    return reason == "finished"


# ── Substitute search & guard ────────────────────────────────────────

# A candidate whose title contains one of these is a different recording,
# not another upload of the same track -- reject it unless the original
# title (or the member's own query) already contained the term.
_VARIANT_TERMS: tuple[str, ...] = (
    "cover",
    "remix",
    "sped up",
    "sped-up",
    "slowed",
    "reverb",
    "nightcore",
    "8d",
    "1 hour",
    "one hour",
    "10 hours",
    "loop",
    "live",
    "instrumental",
    "karaoke",
    "acoustic",
    "mashup",
    "parody",
    "bass boosted",
)
_VARIANT_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (term, re.compile(rf"\b{re.escape(term)}\b")) for term in _VARIANT_TERMS
)

# Upload-decoration words that carry no identity: ignored when comparing
# titles, so "Song (Official Video)" matches "Song [Lyrics]".
_NOISE_WORDS = frozenset(
    "official video audio music lyrics lyric visualizer visualiser"
    " hd hq mv feat ft featuring remastered remaster the a an and".split()
)

_BRACKETED_RE = re.compile(r"[(\[][^)\]]*[)\]]")
_WORD_RE = re.compile(r"[a-z0-9']+")

# Duration guard: ±20% of the original, but never tighter than ±15s so
# short tracks aren't impossible to match.
_LENGTH_TOLERANCE_FRACTION = 0.2
_LENGTH_TOLERANCE_FLOOR_MS = 15_000


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _core_tokens(text: str) -> set[str]:
    return _words(_BRACKETED_RE.sub(" ", text)) - _NOISE_WORDS


def substitute_query(title: str | None, author: str | None) -> str:
    """Search text for finding a replacement upload of a failed track.

    Drops bracketed decorations from the title and YouTube channel-name
    noise from the author (``VEVO`` suffix, `` - Topic`` auto-channels),
    and skips the author entirely when the title already contains it.
    """
    base = _BRACKETED_RE.sub(" ", title or "").strip() or (title or "").strip()
    cleaned = re.sub(r"(?i)vevo$", "", (author or "").strip()).strip()
    cleaned = cleaned.removesuffix(" - Topic").strip()
    if cleaned and not _words(cleaned) <= _words(base):
        return f"{base} {cleaned}".strip()
    return base


def substitute_queries(title: str | None, author: str | None) -> list[str]:
    """Ordered search queries for the recovery chain, most precise first.

    A YouTube track's ``author`` is the *uploader channel*, which for
    re-uploads is unrelated to the song (verified live: a blocked Marvin
    Gaye upload by channel "O.E.U. Studios" searched with the channel
    name returned only that channel's other videos, so no substitute
    survived -- while the bare title found the real track). So: try
    title+author first for precision, then the bare title as the rescue.
    """
    with_author = substitute_query(title, author)
    title_only = _BRACKETED_RE.sub(" ", title or "").strip() or (title or "").strip()
    queries = [q for q in (with_author, title_only) if q]
    return list(dict.fromkeys(queries))


def pick_substitute(
    candidates: Sequence[Any],
    *,
    original_title: str | None,
    original_author: str | None = None,
    original_length_ms: int = 0,
    exclude_identifiers: frozenset[str] | set[str] = frozenset(),
    allowed_terms: str = "",
) -> Any | None:
    """Return the first candidate that plausibly IS the failed track.

    The guard is what keeps the visible-fallback honest: without it the
    top search hit can be an hour-long mix, a 45-second clip, or a
    "sped up" edit. Candidates only need ``title`` / ``author`` /
    ``length`` / ``identifier`` attributes (wavelink Playables qualify).

    Checks, in order: not the failed upload itself; no variant term
    (cover/remix/...) that the original title or ``allowed_terms``
    didn't already contain; duration within ±20% (floor ±15s) when both
    lengths are known; and ≥60% of the original title's core words
    present in the candidate's title+author.
    """
    allowed = f"{original_title or ''} {original_author or ''} {allowed_terms}".lower()
    core = _core_tokens(original_title or "")
    for cand in candidates:
        identifier = str(getattr(cand, "identifier", "") or "")
        if identifier and identifier in exclude_identifiers:
            continue
        cand_title = str(getattr(cand, "title", "") or "")
        cand_author = str(getattr(cand, "author", "") or "")
        lowered = cand_title.lower()
        if any(
            pattern.search(lowered) and not pattern.search(allowed)
            for _term, pattern in _VARIANT_RES
        ):
            continue
        cand_length = int(getattr(cand, "length", 0) or 0)
        if original_length_ms > 0 and cand_length > 0:
            tolerance = max(
                _LENGTH_TOLERANCE_FRACTION * original_length_ms,
                _LENGTH_TOLERANCE_FLOOR_MS,
            )
            if abs(cand_length - original_length_ms) > tolerance:
                continue
        if core:
            cand_tokens = _words(cand_title) | _words(cand_author)
            if len(core & cand_tokens) / len(core) < 0.6:
                continue
        return cand
    return None


def substitution_note(
    original_title: str | None,
    substitute_title: str | None,
    substitute_author: str | None,
    *,
    source: str | None = None,
    reason: str | None = None,
) -> str:
    """The one-line "couldn't play X, playing Y instead" channel note.

    Substitution is deliberately visible: a silent swap that turns out
    to be the wrong upload is its own complaint. Kept Discord-safe the
    same way as ``describe_track_failure``.
    """
    orig = (
        f"**{_clip(original_title, _MAX_TITLE)}**" if original_title else "that track"
    )
    why = f" — {reason}" if reason else ""
    sub = f"**{_clip(substitute_title or 'Unknown', _MAX_TITLE)}"
    if substitute_author:
        sub += f" — {_clip(substitute_author, _MAX_TITLE)}"
    sub += "**"
    if (source or "").lower() == "soundcloud":
        return (
            f"⚠️ Couldn't play {orig}{why}. "
            f"Playing the closest match from SoundCloud instead: {sub}."
        )
    return f"⚠️ Couldn't play {orig}{why}. Playing another upload instead: {sub}."


def track_summary_from_object(track: Any, fallback_author: str | None = None) -> str:
    """Pull fields off a wavelink-like object and format the summary.

    The cog's old static method took a ``wavelink.Playable``; this helper
    keeps that ergonomic for the cog while ``format_track_summary``
    stays usable from tests with plain strings. ``track`` only needs
    ``title`` / ``author`` / ``uri`` attributes (all optional).
    """
    return format_track_summary(
        getattr(track, "title", None),
        getattr(track, "author", None),
        getattr(track, "uri", None),
        fallback_author=fallback_author,
    )
