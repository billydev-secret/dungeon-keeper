"""Wellness panel FastAPI dependencies.

Reads the shared `dk_session` cookie set by the dashboard's OAuth flow on
port 8080. Browsers scope cookies by host (not port) so the same cookie
authenticates users on both 8079 and 8080.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status

from web.auth import (
    SESSION_COOKIE,
    AuthenticatedUser,
    DiscordOAuthAuth,
    resolve_discord_perms,
)

log = logging.getLogger("dungeonkeeper.wellness.web.deps")


def get_ctx(request: Request):
    return request.app.state.ctx


def get_guild_id(request: Request) -> int:
    """Return the active guild_id from the session, with fallback."""
    from web.deps import get_active_guild_id
    return get_active_guild_id(request)


def get_auth(request: Request) -> DiscordOAuthAuth:
    return request.app.state.auth


async def get_current_user(
    request: Request,
    auth: DiscordOAuthAuth = Depends(get_auth),
) -> AuthenticatedUser | None:
    """Resolve the current Discord user from the shared session cookie.

    Returns None when no valid session is present (so pages can render a
    Login button instead of 401-ing).
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    session = auth.read_session(cookie)
    if not session:
        return None

    user_id: int = int(session["uid"])
    username: str = str(session["name"])

    ctx = request.app.state.ctx
    bot = getattr(ctx, "bot", None)
    active_guild_id = session.get("guild_id", ctx.guild_id)
    guild = bot.get_guild(active_guild_id) if bot else None

    if guild:
        member = guild.get_member(user_id)
        if not member:
            return None
        perms = resolve_discord_perms(member.guild_permissions.value)
        return AuthenticatedUser(
            user_id=user_id,
            username=member.display_name,
            perms=perms,
        )

    perms_bits: int = int(session.get("perms_bits", 0))
    return AuthenticatedUser(
        user_id=user_id,
        username=username,
        perms=resolve_discord_perms(perms_bits),
    )


async def require_user(
    user: AuthenticatedUser | None = Depends(get_current_user),
) -> AuthenticatedUser:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required"
        )
    return user


def require_perms(required: set[str]) -> Callable[..., Awaitable[AuthenticatedUser]]:
    required_frozen = frozenset(required)

    async def _dep(
        user: AuthenticatedUser = Depends(require_user),
    ) -> AuthenticatedUser:
        if not required_frozen.issubset(user.perms):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
            )
        return user

    return _dep


require_manage_server = require_perms({"manage_server"})
