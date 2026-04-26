"""Spotify OAuth — bot-owner one-time authorization for private playlist access.

Flow: admin visits /spotify/authorize → redirected to Spotify → consents → Spotify
redirects back to /spotify/callback with a code → we exchange code for tokens
and persist the refresh token in the config KV table. Music cog reads it from
there to mint access tokens on demand.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db_utils import open_db, set_config_value
from web.auth import AuthenticatedUser
from web.deps import require_perms

_log = logging.getLogger("dungeonkeeper.web.spotify_oauth")

SPOTIFY_AUTHORIZE = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"

_STATE_COOKIE = "dk_spotify_oauth_state"

router = APIRouter()


def _base_url() -> str:
    raw = os.getenv("DASHBOARD_BASE_URL", "http://localhost:8080").strip()
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if path.endswith("/callback"):
        path = path[: -len("/callback")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def _redirect_uri() -> str:
    return f"{_base_url()}/spotify/callback"


def _is_secure() -> bool:
    return _base_url().startswith("https://")


@router.get("/spotify/authorize", include_in_schema=False)
async def spotify_authorize(
    _user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> RedirectResponse:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(500, "SPOTIFY_CLIENT_ID not configured")

    state = secrets.token_urlsafe(32)
    url = (
        SPOTIFY_AUTHORIZE
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": _redirect_uri(),
                "scope": SPOTIFY_SCOPES,
                "state": state,
                "show_dialog": "true",
            }
        )
    )
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        _STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
    )
    return response


@router.get("/spotify/callback", include_in_schema=False)
async def spotify_callback(
    request: Request,
    _user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> HTMLResponse:
    cookie_state = request.cookies.get(_STATE_COOKIE)
    query_state = request.query_params.get("state")
    if not cookie_state or cookie_state != query_state:
        raise HTTPException(400, "invalid_state")

    error = request.query_params.get("error")
    if error:
        _log.warning("Spotify authorize denied: %s", error)
        return HTMLResponse(
            f"<h1>Spotify authorization denied</h1><p>{error}</p>",
            status_code=400,
        )

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(400, "missing_code")

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(500, "Spotify credentials not configured")

    auth_value = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")

    async with httpx.AsyncClient(timeout=15.0) as session:
        resp = await session.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
            },
            headers={
                "Authorization": f"Basic {auth_value}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    if resp.status_code != 200:
        _log.warning(
            "Spotify token exchange failed: %d %s", resp.status_code, resp.text[:300]
        )
        raise HTTPException(502, f"Spotify token exchange failed ({resp.status_code})")

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    scope = payload.get("scope", "")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(502, "Spotify response missing refresh_token")

    db_path = request.app.state.ctx.db_path
    with open_db(db_path) as conn:
        set_config_value(conn, "spotify_bot_refresh_token", refresh_token)
        set_config_value(conn, "spotify_bot_scope", scope)

    _log.info("Spotify bot refresh token stored (scope=%s)", scope)
    response = HTMLResponse(
        "<h1>Spotify authorized</h1>"
        "<p>The bot can now access your private playlists. "
        "You can close this tab.</p>"
    )
    response.delete_cookie(_STATE_COOKIE)
    return response
