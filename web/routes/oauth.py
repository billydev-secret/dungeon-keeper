"""Discord OAuth2 login flow — /auth/discord, /callback, /logout."""
from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from web.auth import (
    SESSION_COOKIE,
    DiscordOAuthAuth,
    resolve_discord_perms,
)

_log = logging.getLogger("dungeonkeeper.web.oauth")

DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"

router = APIRouter()


def _base_url() -> str:
    return os.getenv("DASHBOARD_BASE_URL", "http://localhost:8080").rstrip("/")


def _is_secure() -> bool:
    return _base_url().startswith("https://")


# ── Initiate OAuth2 ────────────────────────────────────────────────────

@router.get("/auth/discord", include_in_schema=False)
async def auth_discord(request: Request) -> RedirectResponse:
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    redirect_uri = f"{_base_url()}/callback"
    state = secrets.token_urlsafe(32)

    url = DISCORD_AUTHORIZE + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    })

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        "dk_oauth_state", state,
        max_age=300, httponly=True, samesite="lax", secure=_is_secure(),
    )
    return response


# ── OAuth2 callback ─────────────────────────────────────────────────────

@router.get("/callback", include_in_schema=False)
async def callback(request: Request) -> RedirectResponse:
    # CSRF check — state must match the cookie we set
    cookie_state = request.cookies.get("dk_oauth_state")
    query_state = request.query_params.get("state")
    if not cookie_state or cookie_state != query_state:
        return _login_redirect("Invalid OAuth state. Please try again.")

    code = request.query_params.get("code")
    if not code:
        desc = request.query_params.get("error_description", "Authorization was denied.")
        return _login_redirect(desc)

    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    redirect_uri = f"{_base_url()}/callback"

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Exchange authorization code for access token
        token_resp = await client.post(DISCORD_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })
        if token_resp.status_code != 200:
            _log.warning("Token exchange failed: %s", token_resp.text)
            return _login_redirect("Login failed — could not exchange token.")
        access_token: str = token_resp.json()["access_token"]

        # 2. Fetch user identity
        headers = {"Authorization": f"Bearer {access_token}"}
        user_resp = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
        if user_resp.status_code != 200:
            return _login_redirect("Login failed — could not fetch your Discord profile.")
        user_data = user_resp.json()
        user_id = int(user_data["id"])
        username = user_data.get("global_name") or user_data["username"]

        # 3. Check guild membership + resolve permissions
        ctx = request.app.state.ctx
        guild_id = ctx.guild_id
        permission_bits = 0

        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(guild_id) if bot else None

        if guild:
            member = guild.get_member(user_id)
            if not member:
                return _login_redirect("You must be a member of The Golden Meadow to use this dashboard.")
            permission_bits = member.guild_permissions.value
        else:
            # Standalone mode — check via user's guild list
            guilds_resp = await client.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
            if guilds_resp.status_code != 200:
                return _login_redirect("Login failed — could not verify guild membership.")
            guild_entry = next(
                (g for g in guilds_resp.json() if int(g["id"]) == guild_id),
                None,
            )
            if not guild_entry:
                return _login_redirect("You must be a member of The Golden Meadow to use this dashboard.")
            permission_bits = int(guild_entry.get("permissions", "0"))

    # 4. Create session cookie
    auth: DiscordOAuthAuth = request.app.state.auth  # type: ignore[assignment]
    cookie_value = auth.create_session_cookie(
        user_id=user_id,
        username=username,
        access_token=access_token,
        permission_bits=permission_bits,
    )

    _log.info(
        "Login: %s (id=%d, perms=%s)",
        username, user_id, resolve_discord_perms(permission_bits),
    )

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE, cookie_value,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
        path="/",
    )
    response.delete_cookie("dk_oauth_state")
    return response


# ── Logout ──────────────────────────────────────────────────────────────

@router.get("/logout", include_in_schema=False)
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ── Helpers ─────────────────────────────────────────────────────────────

def _login_redirect(error: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={error}", status_code=302)
