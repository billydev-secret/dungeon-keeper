"""Music playlist pipeline — parse, resolve, dedupe, write, trim.

Ported from OpenMusicBot's ``message_processor.py`` (+ its
``youtube_resolver.py``), rewired onto DK's store (``music_playlist_store``),
config KV (``MusicPlaylistSettings``, the CasinoSettings pattern) and the
bot-owner Spotify client (``services/spotify_resolver``). Per message:

1. **Parse** every supported link (``music_playlist_logic.extract_links``).
2. **Resolve** each to a Spotify track id — direct for track links/URIs,
   by *search* for YouTube (title+channel via the keyless oEmbed endpoint,
   cleaned and scored by ``select_best_match``). An album or playlist link
   contributes exactly its **single most-popular track** — one collection
   post must not flush the whole window, but it shouldn't contribute
   nothing either. A collection the app cannot read (editorial playlist,
   Development-mode null-track reads) goes to the review queue instead.
3. **Dedupe** against the live window; duplicate posts are recorded as
   references (the 🔁 case) so the deletion rule stays honest.
4. **Write** the survivors to Spotify, then **trim** back to ``window_size``.

Confidence gates step 2: at or above ``match_threshold`` the track is added
silently; below it nothing is added and the link lands in the unmatched
review queue with its best candidate and score.

Every Spotify write tolerates :class:`SpotifyResolveError` — a read-only
grant, a 403, or an exhausted 429 surfaces as ``write_blocked`` + an error
string on the summary (the resolver words the read-only case in re-consent
terms), never as a crash in the listener.

A message whose tracks never landed — the playlist write was refused
(``write_failed``) or a link failed to resolve (``resolve_failed``) — is
recorded in the ledger but in a **retryable** status, so it reads as
unprocessed and a rescan re-fires it. That matters because neither failure
inserts a track row, and ``reconcile`` only pushes tracks the DB already
holds: treating these as terminal would silently and permanently drop every
song posted before the owner re-consented, which is a designed-in state
rather than an edge case. Re-processing is safe — anything that did land
dedupes against the live window on the next pass.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

import httpx

from bot_modules.core.db_utils import open_db, parse_bool, set_config_value
from bot_modules.music_playlist import music_playlist_store as store
from bot_modules.music_playlist.music_playlist_logic import (
    LinkType,
    ParsedLink,
    SpotifyTrack,
    YouTubeMetadata,
    clean_youtube_title,
    extract_links,
    infer_artist_and_title,
    parse_spotify_url,
    select_best_match,
)
from bot_modules.services.spotify_resolver import (
    SpotifyResolveError,
    SpotifyResolver,
    SpotifyUnusableReadError,
)

log = logging.getLogger("dungeonkeeper.music_playlist")

SETTINGS_PREFIX = "music_playlist_"

# Unmatched-queue reasons (stored in the ``reason`` column, shown on review).
REASON_NO_METADATA = "youtube_metadata_unavailable"
REASON_NO_CANDIDATES = "no_spotify_candidates"
REASON_LOW_CONFIDENCE = "confidence_below_threshold"
# An album/playlist link whose contents this app cannot read (editorial
# playlist, or the Development-mode null-track shape). Queued rather than
# ledgered retryable: the condition is lasting, and the auto-retry sweep
# re-firing it every backoff step would never converge.
REASON_COLLECTION_UNREADABLE = "collection_unreadable"

# Ledger statuses. Aliased from the store so the retryable-vs-terminal
# classification has exactly one definition (the store's ledger reads it).
STATUS_NO_LINKS = store.STATUS_NO_LINKS
STATUS_WRITE_FAILED = store.STATUS_WRITE_FAILED
STATUS_RESOLVE_FAILED = store.STATUS_RESOLVE_FAILED
STATUS_MESSAGE_GONE = store.STATUS_MESSAGE_GONE

# Auto-retry policy for retryable ledger rows (the cog's timer sweep).
# A row is due at processed_at + BASE * 2^attempts (~5m, 10m, 20m, … from the
# latest failure), giving up after MAX_ATTEMPTS (~10h of coverage) — past
# that, only a manual Re-scan re-fires it, which ignores attempts and remains
# the recovery for long outages like a consent gap. The sweep re-runs
# process_message, so it is write-side only: no playlist read is ever
# involved (reconcile stays a manual button for exactly that reason).
RETRY_BASE_DELAY_S = 300.0
RETRY_MAX_ATTEMPTS = 8
RETRY_BATCH_LIMIT = 10

# Candidate pool cap when resolving a YouTube link: stop searching once this
# many distinct Spotify candidates have been collected (upstream's numbers).
_SEARCH_LIMIT = 7

# Reconcile withholds its removals once it would delete more than this many
# tracks it doesn't recognise. Ordinary drift is one or two rows whose removal
# didn't land; anything past that reads as "this playlist isn't ours".
_RECONCILE_REMOVAL_CONFIRM_AT = 5
_MAX_CANDIDATES = 15

_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
_OEMBED_ATTEMPTS = 3
_OEMBED_TIMEOUT_S = 10.0

ConnectionStatus = Literal["connected", "read_only", "not_connected"]

# Injectable edges, so tests never touch the network: a Spotify track search
# (query, limit) and the YouTube metadata fetch (video id).
SearchTracks = Callable[[str, int], Awaitable[list[SpotifyTrack]]]
YouTubeFetcher = Callable[[str], Awaitable[YouTubeMetadata | None]]
# The retry sweep's Discord edge: (channel_id, message_id) → (content,
# author_id), or None when the message (or its channel) no longer exists.
# Transient fetch failures must RAISE, not return None — None is terminal.
MessageFetcher = Callable[[int, int], Awaitable[tuple[str, int] | None]]


# ── Settings (config KV, the CasinoSettings pattern) ──────────────────


@dataclass(frozen=True)
class MusicPlaylistSettings:
    # Master switch — the feature ships dark until the dashboard flips it.
    enabled: bool = False
    # The one watched channel. 0 = unset (nothing processes).
    channel_id: int = 0
    # Target Spotify playlist id. Empty = unset (nothing processes).
    playlist_id: str = ""
    # Rolling window: song N+1 pushes the oldest out.
    window_size: int = 30
    # YouTube→Spotify confidence gate; below it the link goes to review.
    match_threshold: float = 0.74
    # Deleting the source message pulls the track (unless another live
    # message still references it).
    remove_on_delete: bool = True
    # How far back Re-scan reads the watched channel. This bounds the only
    # recovery path for messages left in a retryable status, so a long
    # read-only stretch on a busy channel needs it raised before the earliest
    # posts fall out of reach.
    rescan_depth: int = 200


DEFAULT_MUSIC_PLAYLIST_SETTINGS = MusicPlaylistSettings()

_BOOL_KEYS = ["enabled", "remove_on_delete"]
_FLOAT_KEYS = ["match_threshold"]
_STR_KEYS = ["playlist_id"]
# Everything else on the dataclass is a plain int.
_INT_KEYS = [
    f.name for f in fields(MusicPlaylistSettings)
    if f.name not in _BOOL_KEYS and f.name not in _FLOAT_KEYS
    and f.name not in _STR_KEYS
]
_ALL_KEYS = frozenset(f.name for f in fields(MusicPlaylistSettings))


def load_music_playlist_settings(
    conn: sqlite3.Connection, guild_id: int
) -> MusicPlaylistSettings:
    """Build MusicPlaylistSettings from stored ``music_playlist_*`` values.

    Guild-scoped only, missing or unparseable values fall back to the
    dataclass defaults; one GLOB query for all keys (the casino loader's
    contract — GLOB treats the underscore literally, unlike LIKE).
    """
    stored = {
        str(r["key"])[len(SETTINGS_PREFIX):]: str(r["value"])
        for r in conn.execute(
            "SELECT key, value FROM config WHERE guild_id = ? "
            "AND key GLOB 'music_playlist_*'",
            (guild_id,),
        )
    }
    defaults = DEFAULT_MUSIC_PLAYLIST_SETTINGS
    kwargs: dict[str, object] = {}
    for key in _BOOL_KEYS:
        raw = stored.get(key, "")
        if raw:
            kwargs[key] = parse_bool(raw, getattr(defaults, key))
    for key in _STR_KEYS:
        raw = stored.get(key, "")
        if raw:
            kwargs[key] = raw
    for key in _FLOAT_KEYS:
        raw = stored.get(key, "")
        if raw:
            try:
                kwargs[key] = float(raw)
            except ValueError:
                pass
    for key in _INT_KEYS:
        raw = stored.get(key, "")
        if raw:
            try:
                kwargs[key] = int(raw)
            except ValueError:
                pass
    if not kwargs:
        return defaults
    for f in defaults.__dataclass_fields__:
        if f not in kwargs:
            kwargs[f] = getattr(defaults, f)
    return MusicPlaylistSettings(**kwargs)  # type: ignore[arg-type]


def save_music_playlist_settings(
    conn: sqlite3.Connection, guild_id: int, values: dict[str, object]
) -> None:
    """Persist a partial dict of settings; unknown keys raise KeyError."""
    unknown = set(values) - _ALL_KEYS
    if unknown:
        raise KeyError(f"unknown music playlist setting(s): {sorted(unknown)}")
    for key, value in values.items():
        stored = ("1" if value else "0") if isinstance(value, bool) else str(value)
        set_config_value(conn, f"{SETTINGS_PREFIX}{key}", stored, guild_id)


# ── Results the cog / dashboard consume ───────────────────────────────


@dataclass(slots=True)
class ProcessingSummary:
    """What one message did to the playlist — drives the source reactions.

    ✅ when ``added_track_ids``, 🔁 when ``duplicate_count``, ❓ when
    ``unmatched_ids``; ``errors`` are already-worded strings safe to log or
    surface (the read-only grant reports itself in re-consent terms).
    """

    links_found: int = 0
    added_track_ids: list[str] = field(default_factory=list)
    duplicate_count: int = 0
    unmatched_ids: list[int] = field(default_factory=list)
    rolled_off_track_ids: list[str] = field(default_factory=list)
    # Nothing was attempted: feature off, wrong channel, or already in the
    # ledger. Distinct from a processed message that merely found no links.
    skipped: bool = False
    # A Spotify playlist write was refused (missing scope / 403 / 429) —
    # the message is ledgered ``write_failed`` and nothing was inserted.
    write_blocked: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrySummary:
    """What one guild's retry sweep did — the cog logs it and reacts from it.

    ``results`` carries (channel_id, message_id, summary) for every message
    that was actually re-processed, so the cog can finally deliver the
    ✅/🔁/❓ reaction the original failure swallowed.
    """

    attempted: int = 0
    recovered: int = 0
    still_failing: int = 0
    gone: int = 0
    results: list[tuple[int, int, ProcessingSummary]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ApproveResult:
    ok: bool
    detail: str
    added_track_id: str | None = None
    was_duplicate: bool = False


@dataclass(frozen=True, slots=True)
class DeleteResult:
    removed_track_ids: list[str]
    errors: list[str]


@dataclass(frozen=True, slots=True)
class _TrackInfo:
    """A resolved candidate with the metadata its window row needs."""

    track_id: str
    title: str
    artist: str
    source_url: str


# ── YouTube metadata (oEmbed — keyless, quota-free) ───────────────────


async def fetch_youtube_metadata(
    video_id: str, *, client: httpx.AsyncClient | None = None
) -> YouTubeMetadata | None:
    """Title + channel for a video via YouTube's oEmbed endpoint, or None.

    None means "couldn't get metadata" in every failure shape — 404, 4xx,
    exhausted retries — and the caller queues the link for review rather
    than dropping it. Retries 5xx/429/transport errors with the upstream
    backoff. Pass ``client`` to reuse a session (tests inject a
    ``httpx.MockTransport`` one).
    """
    source_url = f"https://www.youtube.com/watch?v={video_id}"
    params = {"url": source_url, "format": "json"}
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=_OEMBED_TIMEOUT_S)
    try:
        for attempt in range(_OEMBED_ATTEMPTS):
            try:
                resp = await client.get(_OEMBED_ENDPOINT, params=params)
            except httpx.HTTPError:
                if attempt >= _OEMBED_ATTEMPTS - 1:
                    log.warning("oEmbed lookup exhausted retries for %s", video_id)
                    return None
                await asyncio.sleep((2 ** attempt) + random.uniform(0.0, 0.25))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                try:
                    wait = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                await asyncio.sleep(wait + random.uniform(0.0, 0.25))
                continue
            if resp.status_code >= 500 and attempt < _OEMBED_ATTEMPTS - 1:
                await asyncio.sleep((2 ** attempt) + random.uniform(0.0, 0.25))
                continue
            if resp.status_code >= 400:
                log.warning(
                    "oEmbed lookup failed for %s: %d", video_id, resp.status_code
                )
                return None
            payload = resp.json()
            title = str(payload.get("title", "")).strip()
            channel_name = str(payload.get("author_name", "")).strip() or None
            if not title:
                return None
            return YouTubeMetadata(
                video_id=video_id,
                title=title,
                channel_name=channel_name,
                source_url=source_url,
            )
        return None
    finally:
        if owns_client:
            await client.aclose()


def build_search_queries(metadata: YouTubeMetadata) -> list[str]:
    """Spotify search strings for a YouTube video, most specific first."""
    cleaned_title = clean_youtube_title(metadata.title)
    artist_hint, song_title = infer_artist_and_title(
        cleaned_title, metadata.channel_name
    )
    candidates: list[str] = []
    if artist_hint:
        candidates.append(f"track:{song_title} artist:{artist_hint}")
        candidates.append(f"{artist_hint} {song_title}")
    candidates.append(song_title)
    candidates.append(cleaned_title)
    deduped: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        normalized = " ".join(query.split()).strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        deduped.append(normalized)
    return deduped


async def search_spotify_tracks(
    resolver: SpotifyResolver, query: str, *, limit: int = _SEARCH_LIMIT
) -> list[SpotifyTrack]:
    """Search Spotify's track catalog through the resolver's app client.

    The resolver has no public search yet (it grew playlist writes this
    round); this leans on its ``_ensure_client``/``_call`` internals — same
    retry/429 behavior as every other Spotify call — until a first-class
    ``search_tracks`` lands there.
    """
    client = resolver._ensure_client()  # noqa: SLF001
    payload = await resolver._call(  # noqa: SLF001
        client.search, q=query, type="track", limit=limit
    )
    items = ((payload or {}).get("tracks") or {}).get("items") or []
    results: list[SpotifyTrack] = []
    for item in items:
        track_id = item.get("id")
        if not track_id:
            continue
        results.append(
            SpotifyTrack(
                track_id=track_id,
                name=item.get("name", ""),
                artists=[a.get("name", "") for a in item.get("artists", [])],
                url=item.get("external_urls", {}).get("spotify"),
            )
        )
    return results


# ── The pipeline ──────────────────────────────────────────────────────


class MusicPlaylistService:
    """The watched-channel pipeline over one guild's rolling playlist."""

    def __init__(
        self,
        db_path: Path,
        spotify: SpotifyResolver,
        *,
        youtube_fetcher: YouTubeFetcher | None = None,
        search_tracks: SearchTracks | None = None,
    ) -> None:
        self._db_path = db_path
        self._spotify = spotify
        self._fetch_youtube: YouTubeFetcher = youtube_fetcher or fetch_youtube_metadata
        if search_tracks is not None:
            self._search: SearchTracks = search_tracks
        else:
            async def _default_search(query: str, limit: int) -> list[SpotifyTrack]:
                return await search_spotify_tracks(spotify, query, limit=limit)
            self._search = _default_search

    # -- settings / health -------------------------------------------------

    async def load_settings(self, guild_id: int) -> MusicPlaylistSettings:
        return await asyncio.to_thread(self._load_settings, guild_id)

    def _load_settings(self, guild_id: int) -> MusicPlaylistSettings:
        with open_db(self._db_path) as conn:
            return load_music_playlist_settings(conn, guild_id)

    async def connection_status(self) -> ConnectionStatus:
        """The dashboard chip: connected / read-only / not connected.

        Derived from the stored grant, so re-consent takes effect without a
        restart: no scopes recorded means no token was ever stored; scopes
        without playlist-modify mean the token predates the scope widening.
        """
        scopes = await self._spotify.playlist_scopes()
        if not scopes:
            return "not_connected"
        if await self._spotify.can_modify_playlists():
            return "connected"
        return "read_only"

    # -- the per-message pipeline ------------------------------------------

    async def process_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        content: str,
        added_by: int,
    ) -> ProcessingSummary:
        """Run one message through parse → resolve → dedupe → write → trim."""
        summary = ProcessingSummary()
        settings = await self.load_settings(guild_id)
        if (
            not settings.enabled
            or not settings.playlist_id
            or not settings.channel_id
            or channel_id != settings.channel_id
        ):
            summary.skipped = True
            return summary
        if await asyncio.to_thread(self._is_processed, guild_id, message_id):
            summary.skipped = True
            return summary

        links = extract_links(content)
        summary.links_found = len(links)
        if not links:
            await asyncio.to_thread(
                self._record_message, guild_id, message_id, channel_id,
                STATUS_NO_LINKS,
            )
            return summary

        # Resolve every link to zero or more candidate tracks. Order is
        # preserved (with repeats) so the partition can count a same-song
        # repeat within one message as a duplicate.
        infos: dict[str, _TrackInfo] = {}
        ordered: list[str] = []
        for link in links:
            try:
                resolved = await self._resolve_link(
                    guild_id, channel_id, message_id, link, settings,
                    added_by, summary,
                )
            except SpotifyResolveError as exc:
                summary.errors.append(str(exc))
                continue
            for info in resolved:
                infos.setdefault(info.track_id, info)
                ordered.append(info.track_id)

        new_ids, existing_ids = await asyncio.to_thread(
            self._partition, guild_id, settings.playlist_id, ordered
        )

        write_ok = True
        if new_ids:
            try:
                await self._spotify.add_tracks_to_playlist(
                    settings.playlist_id, new_ids
                )
            except SpotifyResolveError as exc:
                # Read-only grant, 403, exhausted 429 — the resolver already
                # worded it. Ledger the message as RETRYABLE, not processed:
                # no track row was inserted, so reconcile cannot recover these
                # (it only pushes tracks the DB already holds). A rescan
                # re-fires them once the grant or the outage is fixed.
                write_ok = False
                summary.write_blocked = True
                summary.errors.append(str(exc))

        # At this point summary.errors holds *resolve* failures only (the
        # overflow removal below appends after the ledger is written). A link
        # that errored out resolved to nothing and was not queued for review,
        # so the message has to stay re-firable or a transient Spotify blip
        # silently eats a valid link. Re-processing is safe: tracks that did
        # land dedupe against the live window on the next pass.
        if not write_ok:
            ledger_status = store.STATUS_WRITE_FAILED
        elif summary.errors:
            ledger_status = store.STATUS_RESOLVE_FAILED
        else:
            ledger_status = store.STATUS_PROCESSED

        added, overflow = await asyncio.to_thread(
            self._finalize_message,
            guild_id, channel_id, message_id, settings,
            new_ids if write_ok else [], existing_ids, infos, added_by,
            ledger_status,
        )
        summary.added_track_ids = added
        # Raced inserts (another message landed the track between partition
        # and insert) were downgraded to duplicate references in finalize.
        summary.duplicate_count = len(existing_ids) + (
            len(new_ids) - len(added) if write_ok else 0
        )
        summary.rolled_off_track_ids = [str(r["track_id"]) for r in overflow]
        if overflow:
            await self._remove_from_spotify(
                settings.playlist_id, summary.rolled_off_track_ids,
                summary.errors,
            )
        return summary

    # -- the auto-retry sweep ----------------------------------------------

    async def guilds_with_pending_retries(self) -> list[int]:
        """Guilds the retry sweep should visit at all this tick."""
        def _query() -> list[int]:
            with open_db(self._db_path) as conn:
                return store.guilds_with_retryable(
                    conn, max_attempts=RETRY_MAX_ATTEMPTS
                )
        return await asyncio.to_thread(_query)

    async def retry_failed_messages(
        self, guild_id: int, fetch_message: MessageFetcher
    ) -> RetrySummary:
        """Re-fire due retryable ledger rows through the normal pipeline.

        Write-side only: each row is re-run through ``process_message``, which
        overwrites the ledger row with its real outcome. Attempts are bumped
        *before* re-processing so a crash mid-retry can never hot-loop; a
        still-failing row's refreshed ``processed_at`` restarts its backoff
        window. A message the fetcher reports gone is ledgered terminally.
        """
        result = RetrySummary()
        settings = await self.load_settings(guild_id)
        if (
            not settings.enabled
            or not settings.playlist_id
            or not settings.channel_id
        ):
            return result
        rows = await asyncio.to_thread(self._due_retries, guild_id)
        for row in rows:
            channel_id = int(row["channel_id"])
            message_id = int(row["message_id"])
            await asyncio.to_thread(self._bump_attempts, guild_id, message_id)
            result.attempted += 1
            try:
                fetched = await fetch_message(channel_id, message_id)
            except Exception as exc:
                # Transient Discord failure — the row stays for a later sweep.
                log.warning(
                    "music playlist retry: fetch failed for %s: %s",
                    message_id, exc,
                )
                result.still_failing += 1
                continue
            if fetched is None:
                await asyncio.to_thread(
                    self._record_message, guild_id, message_id, channel_id,
                    store.STATUS_MESSAGE_GONE,
                )
                result.gone += 1
                continue
            content, author_id = fetched
            try:
                summary = await self.process_message(
                    guild_id, channel_id, message_id, content, author_id
                )
            except Exception:
                log.exception(
                    "music playlist retry: processing failed for %s", message_id
                )
                result.still_failing += 1
                continue
            if summary.skipped:
                # A dial moved since the failure (channel/enabled/playlist) —
                # attempted, but there is no outcome to report or react to.
                continue
            result.results.append((channel_id, message_id, summary))
            if summary.write_blocked or summary.errors:
                result.still_failing += 1
            else:
                result.recovered += 1
        return result

    async def _resolve_link(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        link: ParsedLink,
        settings: MusicPlaylistSettings,
        added_by: int,
        summary: ProcessingSummary,
    ) -> list[_TrackInfo]:
        """One link → its candidate tracks (possibly none, possibly queued)."""
        if link.link_type is LinkType.SPOTIFY_TRACK:
            result = await self._spotify.resolve(link.raw_url)
            if not result.tracks:
                return []
            track = result.tracks[0]
            return [_TrackInfo(
                track_id=link.item_id,
                title=track.title,
                artist=", ".join(track.artists),
                source_url=link.raw_url,
            )]

        # Reviewer verdicts are remembered per link, checked before any
        # network work: an approved link re-adds the reviewer's exact track
        # even if the source video has since gone private, and a rejected
        # link stays rejected instead of re-queueing on every re-scan or
        # retry. Track links skip this — a direct id needs no verdict.
        verdict = await asyncio.to_thread(
            self._latest_verdict, guild_id, link.raw_url
        )
        if verdict is not None:
            if (
                verdict["status"] == store.STATUS_APPROVED
                and verdict["candidate_track_id"]
            ):
                return [_TrackInfo(
                    track_id=str(verdict["candidate_track_id"]),
                    title=verdict["candidate_name"] or "Unknown",
                    artist=verdict["candidate_artist"] or "",
                    source_url=link.raw_url,
                )]
            if verdict["status"] == store.STATUS_REJECTED:
                return []
            # Approved without a candidate can't be created today (approve
            # refuses); fall through to a normal resolve if it ever exists.

        if link.link_type in (LinkType.SPOTIFY_ALBUM, LinkType.SPOTIFY_PLAYLIST):
            # A collection contributes exactly its most-popular track: one
            # album post must not flush the window, but it shouldn't
            # contribute nothing either. (popularity is Spotify's 0-100
            # recency-weighted score, not a play count; while the app lacks
            # popularity data the pick degrades to the opening track.)
            try:
                if link.link_type is LinkType.SPOTIFY_ALBUM:
                    track = await self._spotify.album_top_track(link.item_id)
                else:
                    track = await self._spotify.playlist_top_track(link.item_id)
            except SpotifyUnusableReadError as exc:
                # Lasting condition (editorial playlist, unreadable contents)
                # — visible in the review queue, terminal for the ledger. A
                # retryable status here would put it on the auto-retry sweep
                # forever with no chance of a different outcome.
                unmatched_id = await asyncio.to_thread(
                    self._queue_unmatched, guild_id,
                    channel_id=channel_id, message_id=message_id,
                    source_url=link.raw_url, added_by=added_by,
                    reason=REASON_COLLECTION_UNREADABLE,
                )
                summary.unmatched_ids.append(unmatched_id)
                log.info(
                    "music playlist: collection unreadable (%s): %s",
                    link.raw_url, exc,
                )
                return []
            if track is None:  # empty collection
                return []
            # The resolver's track model carries the URL, not the id.
            parsed = parse_spotify_url(track.spotify_url or "")
            if parsed is None or parsed.link_type is not LinkType.SPOTIFY_TRACK:
                return []
            return [_TrackInfo(
                track_id=parsed.item_id,
                title=track.title,
                artist=", ".join(track.artists),
                source_url=link.raw_url,
            )]

        # YouTube: fetch metadata, search Spotify, gate on confidence.
        metadata = await self._fetch_youtube(link.item_id)
        if metadata is None:
            unmatched_id = await asyncio.to_thread(
                self._queue_unmatched, guild_id,
                channel_id=channel_id, message_id=message_id,
                source_url=link.raw_url, added_by=added_by,
                reason=REASON_NO_METADATA,
            )
            summary.unmatched_ids.append(unmatched_id)
            return []

        candidates_by_id: dict[str, SpotifyTrack] = {}
        for query in build_search_queries(metadata):
            for candidate in await self._search(query, _SEARCH_LIMIT):
                candidates_by_id.setdefault(candidate.track_id, candidate)
            if len(candidates_by_id) >= _MAX_CANDIDATES:
                break

        if not candidates_by_id:
            unmatched_id = await asyncio.to_thread(
                self._queue_unmatched, guild_id,
                channel_id=channel_id, message_id=message_id,
                source_url=link.raw_url, added_by=added_by,
                extracted_title=metadata.title,
                extracted_channel=metadata.channel_name,
                reason=REASON_NO_CANDIDATES,
            )
            summary.unmatched_ids.append(unmatched_id)
            return []

        decision = select_best_match(
            metadata, candidates_by_id.values(), settings.match_threshold
        )
        if decision.best_candidate and decision.is_confident:
            track = decision.best_candidate.track
            return [_TrackInfo(
                track_id=track.track_id,
                title=track.name,
                artist=", ".join(track.artists),
                source_url=link.raw_url,
            )]

        best = decision.best_candidate
        unmatched_id = await asyncio.to_thread(
            self._queue_unmatched, guild_id,
            channel_id=channel_id, message_id=message_id,
            source_url=link.raw_url, added_by=added_by,
            extracted_title=metadata.title,
            extracted_channel=metadata.channel_name,
            candidate_track_id=best.track.track_id if best else None,
            candidate_name=best.track.name if best else None,
            candidate_artist=", ".join(best.track.artists) if best else None,
            confidence=best.confidence if best else None,
            reason=REASON_LOW_CONFIDENCE,
        )
        summary.unmatched_ids.append(unmatched_id)
        return []

    # -- review queue ------------------------------------------------------

    async def approve_unmatched(
        self, guild_id: int, item_id: int, reviewed_by: int
    ) -> ApproveResult:
        """Approve a queued item: add its candidate track, then trim.

        The status flip is the exactly-once claim (two reviewers racing
        resolve it once); a failed Spotify write reopens the item so the
        approval can be retried once the grant is fixed.
        """
        settings = await self.load_settings(guild_id)
        if not settings.playlist_id:
            return ApproveResult(ok=False, detail="No target playlist configured.")

        item = await asyncio.to_thread(self._get_unmatched, guild_id, item_id)
        if item is None:
            return ApproveResult(ok=False, detail="Review item not found.")
        if item["status"] != store.STATUS_PENDING:
            return ApproveResult(
                ok=False, detail=f"Already {item['status']}."
            )
        track_id = item["candidate_track_id"]
        if not track_id:
            return ApproveResult(
                ok=False,
                detail="No candidate track to add — reject it instead.",
            )

        claimed, was_duplicate = await asyncio.to_thread(
            self._claim_approval, guild_id, item_id, item, settings,
            str(track_id), reviewed_by,
        )
        if not claimed:
            return ApproveResult(ok=False, detail="Already resolved.")
        if was_duplicate:
            return ApproveResult(
                ok=True,
                detail="Track is already in the window; recorded as a duplicate.",
                added_track_id=str(track_id),
                was_duplicate=True,
            )

        try:
            await self._spotify.add_tracks_to_playlist(
                settings.playlist_id, [str(track_id)]
            )
        except SpotifyResolveError as exc:
            await asyncio.to_thread(self._reopen_unmatched, guild_id, item_id)
            return ApproveResult(ok=False, detail=str(exc))

        overflow_ids = await asyncio.to_thread(
            self._insert_approved, guild_id, item, settings, str(track_id)
        )
        errors: list[str] = []
        if overflow_ids:
            await self._remove_from_spotify(
                settings.playlist_id, overflow_ids, errors
            )
        detail = "Approved and added to the playlist."
        if errors:
            detail += f" (trim incomplete: {errors[0]})"
        return ApproveResult(ok=True, detail=detail, added_track_id=str(track_id))

    async def reject_unmatched(
        self, guild_id: int, item_id: int, reviewed_by: int
    ) -> bool:
        """Reject a queued item. False when it wasn't pending (or isn't ours)."""
        return await asyncio.to_thread(
            self._set_unmatched, guild_id, item_id, store.STATUS_REJECTED,
            reviewed_by,
        )

    # -- deletions ---------------------------------------------------------

    async def reconcile(
        self, guild_id: int, *, confirm_removals: bool = False
    ) -> dict[str, Any]:
        """Square the actual Spotify playlist with the window the DB believes.

        The DB is authoritative both ways: window tracks missing from Spotify
        are added (heals a failed write), Spotify tracks the window doesn't
        hold are removed (heals a best-effort removal that never landed).
        Returns the dict the dashboard's Maintenance button renders verbatim.

        **The removal half is guarded.** "Not in the window" and "not ours" are
        indistinguishable from here, so pointing the playlist dial at an
        existing playlist and clicking Reconcile would delete every song on it
        — irreversibly, since the Spotify removal is a real write. When the
        removal count clears :data:`_RECONCILE_REMOVAL_CONFIRM_AT`, the adds
        still run but the removals are withheld and reported as
        ``needs_confirmation`` for the caller to confirm explicitly.
        """
        settings = await self.load_settings(guild_id)
        if not settings.playlist_id:
            return {"ok": False, "error": "No playlist configured."}
        rows = await asyncio.to_thread(
            self._live_window, guild_id, settings.playlist_id
        )
        want = [str(r["track_id"]) for r in rows]
        withheld = False
        try:
            have = await self._spotify.playlist_track_ids(settings.playlist_id)
            have_set = set(have)
            missing = [t for t in want if t not in have_set]
            extra = sorted(have_set - set(want))
            # A handful of strays is ordinary drift (a removal that didn't
            # land). A pile of them means the playlist holds songs this bot
            # never put there — almost certainly the wrong playlist id.
            withheld = (
                bool(extra)
                and not confirm_removals
                and len(extra) > _RECONCILE_REMOVAL_CONFIRM_AT
            )
            if missing:
                await self._spotify.add_tracks_to_playlist(
                    settings.playlist_id, missing
                )
            if extra and not withheld:
                await self._spotify.remove_tracks_from_playlist(
                    settings.playlist_id, extra
                )
        except SpotifyResolveError as exc:
            return {"ok": False, "error": str(exc)}
        result: dict[str, Any] = {
            "ok": True,
            "in_window": len(want),
            "added": len(missing),
            "removed": 0 if withheld else len(extra),
        }
        if withheld:
            result["needs_confirmation"] = {
                "would_remove": len(extra),
                "sample": extra[:10],
            }
        return result

    async def handle_message_deleted(
        self, guild_id: int, message_id: int
    ) -> DeleteResult:
        """A watched-channel message was deleted; pull its tracks.

        The store applies the two-people-posted-the-same-song rule: a track
        another live message still references stays (re-attributed to that
        message), so only the returned tracks are removed from Spotify.
        No-op when the ``remove_on_delete`` dial is off.
        """
        settings = await self.load_settings(guild_id)
        # Disabled means paused, and the panel and manual both sell it as
        # "stops processing without losing anything". Honouring only
        # remove_on_delete here would let a paused feature still strip tracks
        # out of the playlist whenever someone tidied an old post.
        if not settings.enabled or not settings.remove_on_delete:
            return DeleteResult(removed_track_ids=[], errors=[])
        rows = await asyncio.to_thread(
            self._remove_for_message, guild_id, message_id
        )
        removed_ids = [str(r["track_id"]) for r in rows]
        errors: list[str] = []
        # Rows can only span one playlist today, but group defensively —
        # the platform column exists for a second writer someday.
        by_playlist: dict[str, list[str]] = {}
        for row in rows:
            if row["platform"] != store.PLATFORM_SPOTIFY:
                continue
            by_playlist.setdefault(str(row["playlist_id"]), []).append(
                str(row["track_id"])
            )
        for playlist_id, track_ids in by_playlist.items():
            await self._remove_from_spotify(playlist_id, track_ids, errors)
        return DeleteResult(removed_track_ids=removed_ids, errors=errors)

    # -- Spotify write tolerance -------------------------------------------

    async def _remove_from_spotify(
        self, playlist_id: str, track_ids: list[str], errors: list[str]
    ) -> None:
        """Best-effort playlist removal; the DB is already conservative.

        The rows are marked removed before this runs, so a failure here
        leaves Spotify over-full rather than the DB over-reporting — the
        reconcile action squares it later.
        """
        try:
            await self._spotify.remove_tracks_from_playlist(
                playlist_id, track_ids
            )
        except SpotifyResolveError as exc:
            log.warning(
                "playlist removal failed for %s (%d track(s)): %s",
                playlist_id, len(track_ids), exc,
            )
            errors.append(str(exc))

    # -- sync DB helpers (each its own transaction via open_db) ------------

    def _is_processed(self, guild_id: int, message_id: int) -> bool:
        with open_db(self._db_path) as conn:
            return store.is_message_processed(conn, guild_id, message_id)

    def _record_message(
        self, guild_id: int, message_id: int, channel_id: int, status: str
    ) -> None:
        with open_db(self._db_path) as conn:
            store.record_message(
                conn, guild_id, message_id, channel_id, status=status
            )

    def _due_retries(self, guild_id: int) -> list[Mapping[str, Any]]:
        with open_db(self._db_path) as conn:
            return store.list_retryable_messages(
                conn, guild_id,
                base_delay_s=RETRY_BASE_DELAY_S,
                max_attempts=RETRY_MAX_ATTEMPTS,
                limit=RETRY_BATCH_LIMIT,
            )

    def _bump_attempts(self, guild_id: int, message_id: int) -> None:
        with open_db(self._db_path) as conn:
            store.bump_retry_attempts(conn, guild_id, message_id)

    def _partition(
        self, guild_id: int, playlist_id: str, track_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        with open_db(self._db_path) as conn:
            return store.partition_new_vs_existing(
                conn, guild_id, playlist_id, track_ids
            )

    def _finalize_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        settings: MusicPlaylistSettings,
        new_ids: list[str],
        existing_ids: list[str],
        infos: dict[str, _TrackInfo],
        added_by: int,
        status: str,
    ) -> tuple[list[str], list[Mapping[str, Any]]]:
        """One transaction: duplicate refs, inserts, trim, ledger row.

        Returns (track ids actually inserted, overflow rows to remove from
        Spotify). An insert that races another message's (IntegrityError on
        the live-row index) downgrades to a duplicate reference.
        """
        with open_db(self._db_path) as conn:
            added: list[str] = []
            for track_id in dict.fromkeys(existing_ids):
                info = infos[track_id]
                store.record_duplicate_reference(
                    conn, guild_id,
                    playlist_id=settings.playlist_id, track_id=track_id,
                    title=info.title, artist=info.artist,
                    source_url=info.source_url, channel_id=channel_id,
                    message_id=message_id, added_by=added_by,
                )
            for track_id in new_ids:
                info = infos[track_id]
                try:
                    # A constraint failure rolls back only its own statement,
                    # so catching it here leaves the transaction intact.
                    store.insert_track(
                        conn, guild_id,
                        playlist_id=settings.playlist_id, track_id=track_id,
                        title=info.title, artist=info.artist,
                        source_url=info.source_url, channel_id=channel_id,
                        message_id=message_id, added_by=added_by,
                    )
                    added.append(track_id)
                except sqlite3.IntegrityError:
                    store.record_duplicate_reference(
                        conn, guild_id,
                        playlist_id=settings.playlist_id, track_id=track_id,
                        title=info.title, artist=info.artist,
                        source_url=info.source_url, channel_id=channel_id,
                        message_id=message_id, added_by=added_by,
                    )
            overflow = store.trim_to_window(
                conn, guild_id, settings.playlist_id, settings.window_size
            )
            store.record_message(
                conn, guild_id, message_id, channel_id, status=status
            )
            return added, overflow

    def _latest_verdict(
        self, guild_id: int, source_url: str
    ) -> Mapping[str, Any] | None:
        with open_db(self._db_path) as conn:
            return store.latest_review_verdict(conn, guild_id, source_url)

    def _queue_unmatched(self, guild_id: int, **kwargs: Any) -> int:
        with open_db(self._db_path) as conn:
            return store.create_unmatched(conn, guild_id, **kwargs)

    def _get_unmatched(
        self, guild_id: int, item_id: int
    ) -> Mapping[str, Any] | None:
        # Read-only lookup the store doesn't expose yet; same table, same
        # guild scoping as its writers.
        with open_db(self._db_path) as conn:
            return conn.execute(
                "SELECT * FROM music_playlist_unmatched "
                "WHERE id = ? AND guild_id = ?",
                (item_id, guild_id),
            ).fetchone()

    def _claim_approval(
        self,
        guild_id: int,
        item_id: int,
        item: Mapping[str, Any],
        settings: MusicPlaylistSettings,
        track_id: str,
        reviewed_by: int,
    ) -> tuple[bool, bool]:
        """(claimed, was_duplicate) — flip the status, handle the dup case.

        One transaction: if the claim wins and the track is already live,
        the duplicate reference is recorded here so no Spotify write is
        needed at all.
        """
        with open_db(self._db_path) as conn:
            if not store.set_unmatched_status(
                conn, guild_id, item_id, store.STATUS_APPROVED,
                reviewed_by=reviewed_by,
            ):
                return False, False
            _new, existing = store.partition_new_vs_existing(
                conn, guild_id, settings.playlist_id, [track_id]
            )
            if existing:
                store.record_duplicate_reference(
                    conn, guild_id,
                    playlist_id=settings.playlist_id, track_id=track_id,
                    title=item["candidate_name"] or "Unknown",
                    artist=item["candidate_artist"] or "",
                    source_url=item["source_url"],
                    channel_id=item["channel_id"],
                    message_id=item["message_id"],
                    added_by=item["added_by"],
                )
                return True, True
            return True, False

    def _insert_approved(
        self,
        guild_id: int,
        item: Mapping[str, Any],
        settings: MusicPlaylistSettings,
        track_id: str,
    ) -> list[str]:
        """Window row for an approved item + trim; returns overflow ids."""
        with open_db(self._db_path) as conn:
            try:
                store.insert_track(
                    conn, guild_id,
                    playlist_id=settings.playlist_id, track_id=track_id,
                    title=item["candidate_name"] or "Unknown",
                    artist=item["candidate_artist"] or "",
                    source_url=item["source_url"],
                    channel_id=item["channel_id"],
                    message_id=item["message_id"],
                    added_by=item["added_by"],
                )
            except sqlite3.IntegrityError:
                # Raced a concurrent add of the same track; record the
                # reference and let the live row stand.
                store.record_duplicate_reference(
                    conn, guild_id,
                    playlist_id=settings.playlist_id, track_id=track_id,
                    title=item["candidate_name"] or "Unknown",
                    artist=item["candidate_artist"] or "",
                    source_url=item["source_url"],
                    channel_id=item["channel_id"],
                    message_id=item["message_id"],
                    added_by=item["added_by"],
                )
                return []
            overflow = store.trim_to_window(
                conn, guild_id, settings.playlist_id, settings.window_size
            )
            return [str(r["track_id"]) for r in overflow]

    def _reopen_unmatched(self, guild_id: int, item_id: int) -> None:
        """Roll an approval claim back after a failed Spotify write.

        The store deliberately has no approved→pending transition; this
        exists only for "the claim won but the write failed" and lives here
        beside the write it undoes.
        """
        with open_db(self._db_path) as conn:
            conn.execute(
                "UPDATE music_playlist_unmatched "
                "SET status = ?, reviewed_by = NULL, reviewed_at = NULL "
                "WHERE id = ? AND guild_id = ?",
                (store.STATUS_PENDING, item_id, guild_id),
            )

    def _set_unmatched(
        self, guild_id: int, item_id: int, status: str, reviewed_by: int
    ) -> bool:
        with open_db(self._db_path) as conn:
            return store.set_unmatched_status(
                conn, guild_id, item_id, status, reviewed_by=reviewed_by
            )

    def _remove_for_message(
        self, guild_id: int, message_id: int
    ) -> list[Mapping[str, Any]]:
        with open_db(self._db_path) as conn:
            return store.remove_tracks_for_message(conn, guild_id, message_id)

    def _live_window(
        self, guild_id: int, playlist_id: str
    ) -> list[Mapping[str, Any]]:
        with open_db(self._db_path) as conn:
            return store.live_window(conn, guild_id, playlist_id)
