"""Music cog — resolve Spotify URLs to searchable metadata.

Spotify provides metadata only; LavaSrc handles the actual YouTube mirror via
its providers config. This module exists to:
  * Validate Spotify URLs before sending them to wavelink
  * Page through large playlists/albums (spotipy is sync — every call is
    wrapped in asyncio.to_thread to keep Discord's heartbeat alive)
  * Provide a fallback ``to_search_query`` if LavaSrc is bypassed
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyClientCredentials

log = logging.getLogger("dungeonkeeper.music.spotify")

MAX_PLAYLIST_TRACKS = 500
_PLAYLIST_PAGE_SIZE = 100
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)


_SPOTIFY_URL_RE = re.compile(
    r"(?:https?://)?(?:open\.)?spotify\.com/(track|playlist|album)/([A-Za-z0-9]+)"
    r"|spotify:(track|playlist|album):([A-Za-z0-9]+)"
)


class SpotifyResolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotifyTrack:
    title: str
    artists: list[str]
    duration_ms: int
    isrc: str | None
    spotify_url: str

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""


@dataclass(frozen=True)
class SpotifyResolveResult:
    kind: Literal["track", "playlist", "album"]
    name: str | None
    tracks: list[SpotifyTrack]
    truncated: bool = False


class SpotifyResolver:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._client_id = client_id or os.getenv("SPOTIFY_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv("SPOTIFY_CLIENT_SECRET", "")
        self._client: spotipy.Spotify | None = None

    def _ensure_client(self) -> spotipy.Spotify:
        if self._client is None:
            if not self._client_id or not self._client_secret:
                raise SpotifyResolveError(
                    "Spotify credentials missing (set SPOTIFY_CLIENT_ID and "
                    "SPOTIFY_CLIENT_SECRET in .env)"
                )
            auth = SpotifyClientCredentials(
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
            self._client = spotipy.Spotify(auth_manager=auth, retries=0)
        return self._client

    @staticmethod
    def is_spotify_url(s: str) -> bool:
        return bool(_SPOTIFY_URL_RE.search(s.strip()))

    @staticmethod
    def _parse(url: str) -> tuple[str, str]:
        m = _SPOTIFY_URL_RE.search(url.strip())
        if not m:
            raise SpotifyResolveError("Not a valid Spotify URL")
        kind = m.group(1) or m.group(3)
        ident = m.group(2) or m.group(4)
        return kind, ident

    async def resolve(self, url: str) -> SpotifyResolveResult:
        kind, ident = self._parse(url)
        client = self._ensure_client()

        if kind == "track":
            data = await self._call(client.track, ident)
            return SpotifyResolveResult(
                kind="track",
                name=None,
                tracks=[_track_from_api(data)],
            )

        if kind == "album":
            album = await self._call(client.album, ident)
            tracks = await self._page_album_tracks(client, ident)
            return SpotifyResolveResult(
                kind="album",
                name=album.get("name"),
                tracks=tracks,
            )

        if kind == "playlist":
            playlist = await self._call(client.playlist, ident, fields="name")
            tracks, truncated = await self._page_playlist_tracks(client, ident)
            return SpotifyResolveResult(
                kind="playlist",
                name=playlist.get("name"),
                tracks=tracks,
                truncated=truncated,
            )

        raise SpotifyResolveError(f"Unsupported Spotify URL kind: {kind}")

    async def _page_playlist_tracks(
        self, client: spotipy.Spotify, playlist_id: str
    ) -> tuple[list[SpotifyTrack], bool]:
        results: list[SpotifyTrack] = []
        offset = 0
        truncated = False
        while True:
            page = await self._call(
                client.playlist_items,
                playlist_id,
                limit=_PLAYLIST_PAGE_SIZE,
                offset=offset,
                additional_types=("track",),
            )
            for item in page.get("items", []):
                track = item.get("track")
                if not track or track.get("is_local"):
                    continue
                results.append(_track_from_api(track))
                if len(results) >= MAX_PLAYLIST_TRACKS:
                    return results, page.get("next") is not None or page.get(
                        "total", 0
                    ) > MAX_PLAYLIST_TRACKS
            if not page.get("next"):
                break
            offset += _PLAYLIST_PAGE_SIZE
        return results, truncated

    async def _page_album_tracks(
        self, client: spotipy.Spotify, album_id: str
    ) -> list[SpotifyTrack]:
        results: list[SpotifyTrack] = []
        offset = 0
        while True:
            page = await self._call(
                client.album_tracks,
                album_id,
                limit=50,
                offset=offset,
            )
            for item in page.get("items", []):
                # Album tracks lack ISRC; refetch full track for ISRC if needed.
                # For v1, omit ISRC for album entries — LavaSrc will fall back
                # to title+artist search.
                results.append(
                    SpotifyTrack(
                        title=item.get("name", "Unknown"),
                        artists=[a["name"] for a in item.get("artists", [])],
                        duration_ms=int(item.get("duration_ms") or 0),
                        isrc=None,
                        spotify_url=item.get("external_urls", {}).get(
                            "spotify",
                            f"https://open.spotify.com/track/{item.get('id', '')}",
                        ),
                    )
                )
            if not page.get("next"):
                break
            offset += 50
        return results

    async def _call(self, fn, *args, **kwargs):
        """Invoke a sync spotipy method off-loop, retrying on 429."""
        for attempt, backoff in enumerate((*_RETRY_BACKOFF_S, None)):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except SpotifyException as exc:
                if exc.http_status == 404:
                    raise SpotifyResolveError(
                        "Playlist is private or doesn't exist"
                    ) from exc
                if exc.http_status == 429 and backoff is not None:
                    log.warning(
                        "spotify 429, backing off %.1fs (attempt %d)",
                        backoff,
                        attempt + 1,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise SpotifyResolveError(
                    f"Spotify API error: {exc.http_status} {exc.msg}"
                ) from exc
        raise SpotifyResolveError("Spotify rate-limited; gave up after retries")

    @staticmethod
    def to_search_query(track: SpotifyTrack) -> str:
        """Build a search string suitable for wavelink.Playable.search().

        Wavelink prepends the source prefix (ytsearch:/ytmsearch:) itself based
        on the ``source=`` argument, so do NOT include one here -- doing so
        produces a literal doubled prefix like ``ytmsearch:ytsearch:...`` that
        YouTube searches for verbatim and returns junk results.
        """
        if track.isrc:
            return f'"{track.isrc}"'
        artist = track.primary_artist
        if artist:
            return f"{track.title} {artist}"
        return track.title


def _track_from_api(data: dict) -> SpotifyTrack:
    isrc = data.get("external_ids", {}).get("isrc") if isinstance(data, dict) else None
    return SpotifyTrack(
        title=data.get("name", "Unknown"),
        artists=[a["name"] for a in data.get("artists", [])],
        duration_ms=int(data.get("duration_ms") or 0),
        isrc=isrc,
        spotify_url=data.get("external_urls", {}).get(
            "spotify",
            f"https://open.spotify.com/track/{data.get('id', '')}",
        ),
    )
