"""Wellness panel FastAPI dependencies.

Supports the same auth backends as the main dashboard:

* ``OpenAuth`` for trusted LAN mode.
* ``DiscordOAuthAuth`` for cookie-backed Discord OAuth sessions.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status

from web.auth import (
    AuthBackend,
    AuthenticatedUser,
)

log = logging.getLogger("dungeonkeeper.wellness.web.deps")


def get_ctx(request: Request):
    return request.app.state.ctx


def get_guild_id(request: Request) -> int:
    """Return the active guild_id from the session, with fallback."""
    from web.deps import get_active_guild_id
    return get_active_guild_id(request)


def get_auth(request: Request) -> AuthBackend:
    return request.app.state.auth


async def get_current_user(
    request: Request,
    auth: AuthBackend = Depends(get_auth),
) -> AuthenticatedUser | None:
    """Resolve the current dashboard user from the configured auth backend.

    Returns None when no valid session is present (so pages can render a
    Login button instead of 401-ing).
    """
    return await auth.authenticate(request)


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
