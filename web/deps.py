"""FastAPI dependency-injection factories for the dashboard."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from fastapi import Depends, HTTPException, Request, status

from web.auth import AuthBackend, AuthenticatedUser

T = TypeVar("T")


def get_ctx(request: Request):
    """Return the shared ``AppContext`` stashed on the FastAPI app."""
    return request.app.state.ctx


def get_auth(request: Request) -> AuthBackend:
    return request.app.state.auth


async def run_query(fn: Callable[..., T], *args, **kwargs) -> T:
    """Run a blocking DB-touching function off the event loop.

    Usage::

        def _q():
            with ctx.open_db() as conn:
                return reports_data.get_role_growth_data(conn, ...)
        data = await run_query(_q)
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def require_perms(required: set[str]) -> Callable[..., Awaitable[AuthenticatedUser]]:
    """Return a FastAPI dependency that enforces ``required`` permissions."""
    required_frozen = frozenset(required)

    async def _dep(
        request: Request,
        auth: AuthBackend = Depends(get_auth),
    ) -> AuthenticatedUser:
        user = await auth.authenticate(request)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        if not required_frozen.issubset(user.perms):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _dep
