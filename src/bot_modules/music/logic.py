"""Pure decision/format helpers for the music cog.

Everything here takes plain Python inputs and returns plain Python
values -- no Discord, no wavelink, no I/O. The cog assembles its state
(player, queue, settings) and hands the bits these functions need.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

T = TypeVar("T")


# ── URL / query classification ───────────────────────────────────────


def is_search_url(query: str) -> bool:
    """Return True when ``query`` is a URL that wavelink should treat verbatim.

    Plain-text queries get the YouTube source prefix prepended by wavelink;
    URLs must be passed through untouched. Match the cog's behaviour:
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
    always_on: bool,
) -> bool:
    """Decide whether the idle watcher should drop the voice connection.

    The cog used to inline this across two helpers (``_can_idle_disconnect``
    and the gate inside ``_idle_disconnect``). Hoisting it keeps the
    matrix testable: 24/7 channels never disconnect; if humans are still
    listening to something that's playing or paused, we wait; otherwise
    (empty channel, or nothing playing) we drop.
    """
    if always_on:
        return False
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


# ── /247 toggle message ──────────────────────────────────────────────


def format_247_toggle_message(
    *,
    enabled: bool,
    channel_mention: str,
    cleared_mentions: Sequence[str] = (),
    autoplay_saved: bool = False,
    join_error: str | None = None,
) -> str:
    """Assemble the response for ``/247``.

    Pulled from ``MusicCog.cmd_247`` so each branch (enabled vs disabled,
    with vs without prior 24/7 channels cleared, autoplay flag, join
    failure tail) gets its own row in the test matrix instead of one
    monster integration test.
    """
    if not enabled:
        return f"24/7 disabled for {channel_mention}."

    parts = [f"24/7 enabled for {channel_mention}."]
    if cleared_mentions:
        parts.append(
            "Disabled previous 24/7 channel(s): " + ", ".join(cleared_mentions) + "."
        )
    if autoplay_saved:
        parts.append("Autoplay playlist saved.")
    if join_error:
        parts.append(f"(Couldn't join right now: {join_error})")
    return "\n".join(parts)


def format_247_status_line(channel_mention: str, has_autoplay: bool) -> str:
    """One bullet for ``/247_status`` -- ``• <#id> (autoplay)`` style."""
    suffix = " (autoplay)" if has_autoplay else ""
    return f"• {channel_mention}{suffix}"


# ── Autoplay selection ───────────────────────────────────────────────


def shuffled_autoplay_pool(
    candidates: Iterable[T],
    *,
    cap: int | None = None,
    rng: random.Random | None = None,
) -> list[T]:
    """Shuffle the candidate pool for autoplay refill.

    Used by ``_autoplay_refill`` -- the per-track search loop has to stay
    in the cog (it's async / wavelink-bound) but the random pick is pure.
    Tests pass ``rng=random.Random(seed)`` for deterministic output.

    ``cap`` optionally trims the returned pool; left as ``None`` the
    full shuffled candidate list comes back so the caller can stop
    after N successful searches (some Spotify entries fail to mirror to
    YouTube). A negative ``cap`` returns an empty list.
    """
    items = list(candidates)
    use = rng if rng is not None else random
    use.shuffle(items)
    if cap is None:
        return items
    if cap < 0:
        return []
    return items[:cap]


# ── Helpers callers use to feed format_track_summary ─────────────────


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
