"""Pure decision/format helpers for the music cog.

Everything here takes plain Python inputs and returns plain Python
values -- no Discord, no wavelink, no I/O. The cog assembles its state
(player, queue, settings) and hands the bits these functions need.
"""

from __future__ import annotations

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
    haystack = f"{message or ''}\n{cause or ''}".lower()
    for needle, reason in _FAILURE_REASONS:
        if needle in haystack:
            return f"⚠️ Couldn't play {label} — {reason}. Skipping."
    # Unknown failure: keep the first line of Lavalink's message as a hint.
    detail = (message or "").strip().splitlines()[0] if message else ""
    if detail:
        detail = _clip(detail.rstrip("."), _MAX_DETAIL)
        return f"⚠️ Couldn't play {label} — {detail}. Skipping."
    return f"⚠️ Couldn't play {label} — playback failed. Skipping."


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
