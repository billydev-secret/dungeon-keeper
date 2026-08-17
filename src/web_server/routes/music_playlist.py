"""Music Playlist dashboard API — the feature's whole admin surface.

The watched-channel → rolling-Spotify-playlist feature is dashboard-managed
(no slash commands): the connection chip, the watch/behavior dials, the live
window, the unmatched review queue, history, and the two maintenance actions.
Settings ride the config KV through the service module's load/save pair;
window and queue reads go straight to ``music_playlist_store``; approve /
reject / Spotify writes go through :class:`MusicPlaylistService` so the
exactly-once claim and write-tolerance rules live in one place.

Snowflakes go out as strings — a channel, message, or member id past 2^53
loses precision as a bare JSON number (see the dashboard's snowflake
sweep). Spotify ids are strings already.

The re-scan and reconcile actions need the live bot (channel history /
playlist reads), so they delegate to the cog via ``ctx.bot`` and report 503
when it isn't up — same posture as chat_revive's Discord-side actions.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bot_modules.music_playlist import music_playlist_store as store
from bot_modules.music_playlist.music_playlist_logic import (
    LinkType,
    parse_spotify_url,
)
from bot_modules.music_playlist.music_playlist_service import (
    MusicPlaylistService,
    MusicPlaylistSettings,
    load_music_playlist_settings,
    save_music_playlist_settings,
)
from bot_modules.services.spotify_resolver import (
    SpotifyResolveError,
    SpotifyResolver,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()
_ADMIN = Depends(require_perms({"admin"}))

_HISTORY_LIMIT_DEFAULT = 200
_HISTORY_LIMIT_MAX = 500

# A bare Spotify playlist id (base62). Links/URIs are parsed instead.
_PLAYLIST_ID_RE = re.compile(r"^[0-9A-Za-z]{1,64}$")


def _resolver(ctx) -> SpotifyResolver:
    """The bot-owner Spotify client; module-level so tests can stub it."""
    return SpotifyResolver(db_path=Path(ctx.db_path))


def _service(ctx) -> MusicPlaylistService:
    return MusicPlaylistService(Path(ctx.db_path), _resolver(ctx))


# ── serialization ─────────────────────────────────────────────────────


def _settings_json(s: MusicPlaylistSettings) -> dict:
    return {
        "enabled": s.enabled,
        "channel_id": str(s.channel_id) if s.channel_id else None,
        "playlist_id": s.playlist_id,
        "window_size": s.window_size,
        "match_threshold": s.match_threshold,
        "expand_albums": s.expand_albums,
        "remove_on_delete": s.remove_on_delete,
        "rescan_depth": s.rescan_depth,
    }


def _track_json(r: Mapping[str, Any]) -> dict:
    return {
        "id": int(r["id"]),
        "platform": r["platform"],
        "playlist_id": r["playlist_id"],
        "track_id": r["track_id"],
        "title": r["title"],
        "artist": r["artist"],
        "source_url": r["source_url"],
        "channel_id": str(r["channel_id"]),
        "message_id": str(r["message_id"]),
        "added_by": str(r["added_by"]),
        "added_at": r["added_at"],
        "removed_at": r["removed_at"],
        "removal_reason": r["removal_reason"],
    }


def _unmatched_json(r: Mapping[str, Any]) -> dict:
    return {
        "id": int(r["id"]),
        "channel_id": str(r["channel_id"]),
        "message_id": str(r["message_id"]),
        "source_url": r["source_url"],
        "extracted_title": r["extracted_title"],
        "extracted_channel": r["extracted_channel"],
        "candidate_track_id": r["candidate_track_id"],
        "candidate_name": r["candidate_name"],
        "candidate_artist": r["candidate_artist"],
        "confidence": r["confidence"],
        "reason": r["reason"],
        "added_by": str(r["added_by"]),
        "status": r["status"],
        "created_at": r["created_at"],
    }


# ── status ────────────────────────────────────────────────────────────


@router.get("/music-playlist/status")
async def get_status(request: Request, _: AuthenticatedUser = _ADMIN):
    """The panel's opening read: connection chip + settings + queue sizes."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    connection = await _service(ctx).connection_status()

    def _q():
        with ctx.open_db() as conn:
            settings = load_music_playlist_settings(conn, guild_id)
            window_count = (
                len(store.live_window(conn, guild_id, settings.playlist_id))
                if settings.playlist_id
                else 0
            )
            pending_count = len(store.list_pending(conn, guild_id))
        return settings, window_count, pending_count

    settings, window_count, pending_count = await run_query(_q)
    return {
        "connection": connection,
        "settings": _settings_json(settings),
        "window_count": window_count,
        "pending_count": pending_count,
    }


# ── settings ──────────────────────────────────────────────────────────


class SettingsBody(BaseModel):
    """Partial update — absent fields are left as they are.

    ``channel_id`` is a snowflake *string* (JS can't hold one as a number);
    ``playlist_id`` accepts a bare id, an open.spotify.com/playlist link, or
    a ``spotify:playlist:`` URI. Empty string clears either.
    """

    enabled: bool | None = None
    channel_id: str | None = None
    playlist_id: str | None = None
    window_size: int | None = Field(None, ge=1, le=200)
    match_threshold: float | None = Field(None, ge=0.0, le=1.0)
    expand_albums: bool | None = None
    remove_on_delete: bool | None = None
    # Discord's history() tops out well above this; the ceiling is about not
    # letting one Re-scan click walk an entire channel.
    rescan_depth: int | None = Field(None, ge=1, le=2000)


def _parse_channel_id(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return 0
    try:
        channel_id = int(raw)
    except ValueError:
        channel_id = -1
    if channel_id < 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "channel_id must be a Discord channel id string.",
        )
    return channel_id


def _parse_playlist_id(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if _PLAYLIST_ID_RE.match(raw):
        return raw
    parsed = parse_spotify_url(raw)
    if parsed is None or parsed.link_type is not LinkType.SPOTIFY_PLAYLIST:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "playlist_id must be a Spotify playlist id or playlist link.",
        )
    return parsed.item_id


@router.put("/music-playlist/settings")
async def put_settings(
    request: Request, body: SettingsBody, _: AuthenticatedUser = _ADMIN
):
    values: dict[str, object] = {}
    if body.enabled is not None:
        values["enabled"] = body.enabled
    if body.channel_id is not None:
        values["channel_id"] = _parse_channel_id(body.channel_id)
    if body.playlist_id is not None:
        values["playlist_id"] = _parse_playlist_id(body.playlist_id)
    if body.window_size is not None:
        values["window_size"] = body.window_size
    if body.rescan_depth is not None:
        values["rescan_depth"] = body.rescan_depth
    if body.match_threshold is not None:
        values["match_threshold"] = body.match_threshold
    if body.expand_albums is not None:
        values["expand_albums"] = body.expand_albums
    if body.remove_on_delete is not None:
        values["remove_on_delete"] = body.remove_on_delete

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if values:
                save_music_playlist_settings(conn, guild_id, values)
            return load_music_playlist_settings(conn, guild_id)

    return {"settings": _settings_json(await run_query(_q))}


# ── the live window ───────────────────────────────────────────────────


@router.get("/music-playlist/window")
async def get_window(request: Request, _: AuthenticatedUser = _ADMIN):
    """The live tracks, newest first (the store's own ordering)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            settings = load_music_playlist_settings(conn, guild_id)
            rows = (
                store.live_window(conn, guild_id, settings.playlist_id)
                if settings.playlist_id
                else []
            )
        return settings, rows

    settings, rows = await run_query(_q)
    return {
        "window_size": settings.window_size,
        "tracks": [_track_json(r) for r in rows],
    }


@router.delete("/music-playlist/window/{row_id}")
async def remove_window_track(
    request: Request, row_id: int, _: AuthenticatedUser = _ADMIN
):
    """Admin removal of one live track.

    DB first, Spotify second — the same conservative ordering as the
    service's trim/delete paths: the window never over-reports what's live,
    and a failed Spotify call reports itself while reconcile squares the
    playlist later. (Direct SQL: the store's removal paths are keyed by
    message, not row — the store owner could absorb an ``admin_remove``.)
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT * FROM music_playlist_tracks "
                "WHERE id = ? AND guild_id = ? AND removed_at IS NULL",
                (row_id, guild_id),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            conn.execute(
                "UPDATE music_playlist_tracks "
                "SET removed_at = ?, removal_reason = ? WHERE id = ?",
                (now, store.REASON_ADMIN, row_id),
            )
            # The track's tenure in the window is over, so any outstanding
            # duplicate references to it are stale — leaving them alive lets a
            # later deletion resurrect the track the admin just pulled.
            store.retire_duplicate_references(
                conn,
                guild_id,
                playlist_id=str(row["playlist_id"]),
                track_id=str(row["track_id"]),
                platform=str(row["platform"]),
                removed_at=now,
            )
            return row

    row = await run_query(_q)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such live track.")
    error: str | None = None
    if row["platform"] == store.PLATFORM_SPOTIFY:
        try:
            await _resolver(ctx).remove_tracks_from_playlist(
                str(row["playlist_id"]), [str(row["track_id"])]
            )
        except SpotifyResolveError as exc:
            error = str(exc)
    return {
        "ok": True,
        "removed_track_id": str(row["track_id"]),
        "spotify_removed": error is None,
        "error": error,
    }


# ── unmatched review queue ────────────────────────────────────────────


@router.get("/music-playlist/unmatched")
async def get_unmatched(request: Request, _: AuthenticatedUser = _ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return [_unmatched_json(r) for r in store.list_pending(conn, guild_id)]

    return {"items": await run_query(_q)}


@router.post("/music-playlist/unmatched/{item_id}/approve")
async def approve_unmatched(
    request: Request, item_id: int, user: AuthenticatedUser = _ADMIN
):
    """Approve via the service — status flip is the exactly-once claim.

    A refused Spotify write (read-only grant, 403, 429) comes back 409 with
    the resolver's already-worded message; the service reopened the item, so
    the approval can simply be retried after re-consent.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    result = await _service(ctx).approve_unmatched(guild_id, item_id, user.user_id)
    if not result.ok:
        code = (
            status.HTTP_404_NOT_FOUND
            if result.detail == "Review item not found."
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, result.detail)
    return {
        "ok": True,
        "detail": result.detail,
        "added_track_id": result.added_track_id,
        "was_duplicate": result.was_duplicate,
    }


@router.post("/music-playlist/unmatched/{item_id}/reject")
async def reject_unmatched(
    request: Request, item_id: int, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    ok = await _service(ctx).reject_unmatched(guild_id, item_id, user.user_id)
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No pending review item with that id."
        )
    return {"ok": True}


# ── history ───────────────────────────────────────────────────────────


@router.get("/music-playlist/history")
async def get_history(
    request: Request,
    limit: int = _HISTORY_LIMIT_DEFAULT,
    _: AuthenticatedUser = _ADMIN,
):
    """Rolled-off and removed tracks with their reason, newest removal first.

    Duplicate-reference rows are bookkeeping for the deletion rule, not
    removals — they never show here. (Read-only direct SQL; the store's
    reads are window-shaped, and history is the dashboard's question.)
    """
    limit = max(1, min(limit, _HISTORY_LIMIT_MAX))
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # Bookkeeping rows (duplicate references, and references retired
            # as stale) never occupied the window, so they are not history.
            bookkeeping = sorted(store.BOOKKEEPING_REASONS)
            ph = ", ".join("?" * len(bookkeeping))
            return conn.execute(
                "SELECT * FROM music_playlist_tracks "
                "WHERE guild_id = ? AND removed_at IS NOT NULL "
                f"AND IFNULL(removal_reason, '') NOT IN ({ph}) "
                "ORDER BY removed_at DESC, id DESC LIMIT ?",
                (guild_id, *bookkeeping, limit),
            ).fetchall()

    return {"history": [_track_json(r) for r in await run_query(_q)]}


# ── maintenance ───────────────────────────────────────────────────────


def _cog_action(ctx, method: str):
    """Resolve a maintenance coroutine off the live cog, or None.

    Re-scan needs channel history and reconcile needs the live playlist —
    both are the cog's business (it owns the shared service wired with the
    bot's resolver); the route is just the button.
    """
    bot = getattr(ctx, "bot", None)
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog("MusicPlaylistCog") if get_cog else None
    return getattr(cog, method, None)


async def _run_maintenance(
    request: Request, method: str, **kwargs: Any
) -> dict:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    fn = _cog_action(ctx, method)
    if fn is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bot is not connected — try again once it's back up.",
        )
    result = await fn(guild_id, **kwargs)
    return result if isinstance(result, dict) else {"ok": True}


@router.post("/music-playlist/rescan")
async def rescan(request: Request, _: AuthenticatedUser = _ADMIN):
    """Re-run the pipeline over the watched channel's recent messages."""
    return await _run_maintenance(request, "rescan_channel")


@router.post("/music-playlist/reconcile")
async def reconcile(
    request: Request,
    confirm_removals: bool = False,
    _: AuthenticatedUser = _ADMIN,
):
    """Square the actual Spotify playlist with the window the DB believes.

    Removing a large number of unrecognised tracks needs
    ``?confirm_removals=true`` — without it the response carries
    ``needs_confirmation`` and nothing is deleted. See the service for why.
    """
    return await _run_maintenance(
        request, "reconcile_playlist", confirm_removals=confirm_removals
    )
