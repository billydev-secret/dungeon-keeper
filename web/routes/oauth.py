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


_RETURN_TO_COOKIE = "dk_oauth_return"


def _safelisted_return_urls() -> list[str]:
    """Origins allowed as `return_to` after OAuth callback. Must match scheme+host+port."""
    return [_base_url()]


def _is_safe_return_to(value: str | None) -> bool:
    if not value:
        return False
    safelist = _safelisted_return_urls()
    for prefix in safelist:
        if value == prefix or value.startswith(prefix + "/"):
            return True
    return False


# ── Initiate OAuth2 ────────────────────────────────────────────────────


@router.get("/auth/discord", include_in_schema=False)
async def auth_discord(request: Request) -> RedirectResponse:
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    redirect_uri = f"{_base_url()}/callback"
    state = secrets.token_urlsafe(32)

    url = (
        DISCORD_AUTHORIZE
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify guilds",
                "state": state,
            }
        )
    )

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        "dk_oauth_state",
        state,
        max_age=300,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
    )

    # Honor optional return_to (used by the wellness panel on 8079)
    return_to = request.query_params.get("return_to")
    if return_to and _is_safe_return_to(return_to):
        response.set_cookie(
            _RETURN_TO_COOKIE,
            return_to,
            max_age=300,
            httponly=True,
            samesite="lax",
            secure=_is_secure(),
        )
    return response


# ── OAuth2 callback ─────────────────────────────────────────────────────


@router.get("/callback", include_in_schema=False)
async def callback(request: Request) -> RedirectResponse:
    # CSRF check — state must match the cookie we set
    cookie_state = request.cookies.get("dk_oauth_state")
    query_state = request.query_params.get("state")
    if not cookie_state or cookie_state != query_state:
        return _login_redirect("invalid_state")

    code = request.query_params.get("code")
    if not code:
        # Log the raw error for debugging, but show a safe predefined message
        raw_error = request.query_params.get("error", "unknown")
        _log.warning("OAuth denied: error=%s", raw_error)
        return _login_redirect("denied")

    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    redirect_uri = f"{_base_url()}/callback"

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Exchange authorization code for access token
        token_resp = await client.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if token_resp.status_code != 200:
            _log.warning("Token exchange failed: status=%d", token_resp.status_code)
            return _login_redirect("token_failed")
        access_token: str = token_resp.json()["access_token"]

        # 2. Fetch user identity
        headers = {"Authorization": f"Bearer {access_token}"}
        user_resp = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
        if user_resp.status_code != 200:
            return _login_redirect("profile_failed")
        user_data = user_resp.json()
        user_id = int(user_data["id"])
        username = user_data.get("global_name") or user_data["username"]

        # 3. Build mutual guild list + resolve permissions for active guild
        ctx = request.app.state.ctx
        primary_guild_id = ctx.guild_id
        permission_bits = 0

        bot = getattr(ctx, "bot", None)

        role_ids: list[int] = []
        role_names: list[str] = []
        mutual_guilds: list[dict] = []

        if bot and bot.guilds:
            # Build list of guilds the user shares with the bot
            for g in bot.guilds:
                member = g.get_member(user_id)
                if member:
                    mutual_guilds.append({
                        "id": g.id,
                        "name": g.name,
                        "icon": str(g.icon.url) if g.icon else None,
                    })

            if not mutual_guilds:
                return _login_redirect("no_shared_guild")

            # Default to primary guild if user is in it, else first mutual
            if any(g["id"] == primary_guild_id for g in mutual_guilds):
                active_guild_id = primary_guild_id
            else:
                active_guild_id = mutual_guilds[0]["id"]

            # Resolve permissions for the active guild
            active_guild = bot.get_guild(active_guild_id)
            if active_guild:
                member = active_guild.get_member(user_id)
                if member:
                    permission_bits = member.guild_permissions.value
                    role_ids = [r.id for r in member.roles if not r.is_default()]
                    role_names = [r.name for r in member.roles if not r.is_default()]
        else:
            # Standalone mode — check via user's guild list
            guilds_resp = await client.get(
                f"{DISCORD_API}/users/@me/guilds", headers=headers
            )
            if guilds_resp.status_code != 200:
                return _login_redirect("guilds_failed")
            # In standalone mode we only know about the primary guild
            guild_entry = next(
                (g for g in guilds_resp.json() if int(g["id"]) == primary_guild_id),
                None,
            )
            if not guild_entry:
                return _login_redirect("no_shared_guild")
            permission_bits = int(guild_entry.get("permissions", "0"))
            active_guild_id = primary_guild_id
            mutual_guilds = [{
                "id": int(guild_entry["id"]),
                "name": guild_entry.get("name", str(guild_entry["id"])),
                "icon": None,
            }]

    # 4. Create session cookie
    auth: DiscordOAuthAuth = request.app.state.auth  # type: ignore[assignment]
    cookie_value = auth.create_session_cookie(
        user_id=user_id,
        username=username,
        access_token=access_token,
        permission_bits=permission_bits,
        role_ids=role_ids,
        role_names=role_names,
        guild_id=active_guild_id,
        guilds=mutual_guilds,
    )

    _log.info(
        "Login: %s (id=%d, perms=%s)",
        username,
        user_id,
        resolve_discord_perms(permission_bits),
    )

    # Honor a safelisted return_to from the original /auth/discord call.
    # Defaults to the dashboard root.
    return_to = request.cookies.get(_RETURN_TO_COOKIE)
    final_redirect: str = (
        return_to if return_to and _is_safe_return_to(return_to) else "/"
    )

    response = RedirectResponse(final_redirect, status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
        path="/",
    )
    response.delete_cookie("dk_oauth_state")
    response.delete_cookie(_RETURN_TO_COOKIE)
    return response


# ── Logout ──────────────────────────────────────────────────────────────


@router.get("/logout", include_in_schema=False)
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ── Helpers ─────────────────────────────────────────────────────────────


_ERROR_MESSAGES: dict[str, str] = {
    "invalid_state": "Invalid OAuth state. Please try again.",
    "denied": "Authorization was denied.",
    "token_failed": "Login failed — could not exchange token.",
    "profile_failed": "Login failed — could not fetch your Discord profile.",
    "guilds_failed": "Login failed — could not verify guild membership.",
    "no_shared_guild": "You must share a server with the bot to use this dashboard.",
}


def _login_redirect(error_code: str) -> RedirectResponse:
    return RedirectResponse(
        f"/login?error={urlencode({'': error_code})[1:]}",
        status_code=302,
    )
