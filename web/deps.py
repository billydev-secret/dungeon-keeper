"""FastAPI dependency-injection factories for the dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TypeVar
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status

from web.auth import AuthBackend, AuthenticatedUser

T = TypeVar("T")

_log = logging.getLogger("dungeonkeeper.web.cache")

# ── Report cache ────────────────────────────────────────────────────────

_report_cache: dict[str, tuple[float, str, int, object]] = {}
# key → (expires_at, report_name, guild_id, result)
_DEFAULT_TTL = 3600  # seconds (1 hour — batch warmer refreshes on the same cadence)


def _make_cache_key(name: str, guild_id: int, params: dict) -> str:
    """Build a deterministic cache key from endpoint name + params."""
    raw = json.dumps({"n": name, "g": guild_id, **params}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


async def cached_run_query(
    name: str,
    guild_id: int,
    params: dict,
    fn: Callable[..., T],
    ttl: int = _DEFAULT_TTL,
) -> T:
    """Like ``run_query`` but returns a cached result if one exists and is fresh.

    *name* is a short label for the report (e.g. ``"role-growth"``).
    *params* is a dict of the query-specific parameters used for cache keying.
    """
    key = _make_cache_key(name, guild_id, params)
    now = time.monotonic()

    hit = _report_cache.get(key)
    if hit is not None:
        expires_at, _name, _guild_id, result = hit
        if now < expires_at:
            _log.debug("cache HIT  %s (ttl %.0fs remaining)", name, expires_at - now)
            return result  # type: ignore[return-value]

    result = await asyncio.to_thread(fn)
    _report_cache[key] = (now + ttl, name, guild_id, result)
    _log.debug("cache MISS %s (stored for %ds)", name, ttl)
    return result  # type: ignore[return-value]


def invalidate_report_cache(
    name: str | None = None, guild_id: int | None = None
) -> int:
    """Drop cached entries. With no args, clears everything.

    Returns the number of entries removed.
    """
    if name is None and guild_id is None:
        count = len(_report_cache)
        _report_cache.clear()
        return count

    to_remove = [
        key
        for key, (_ts, entry_name, entry_guild_id, _result) in _report_cache.items()
        if (name is None or entry_name == name)
        and (guild_id is None or entry_guild_id == guild_id)
    ]
    for key in to_remove:
        _report_cache.pop(key, None)
    return len(to_remove)


def store_report_result(
    name: str, guild_id: int, params: dict, result: object, ttl: int = _DEFAULT_TTL
) -> None:
    """Write a pre-computed result directly into the report cache.

    Used by the batch warmer in ``dungeonkeeper.py`` to populate the cache
    proactively so dashboard page loads are always instant.
    """
    key = _make_cache_key(name, guild_id, params)
    _report_cache[key] = (time.monotonic() + ttl, name, guild_id, result)


def get_ctx(request: Request):
    """Return the shared ``AppContext`` stashed on the FastAPI app."""
    return request.app.state.ctx


def get_active_guild_id(request: Request) -> int:
    """Return the active guild_id from the user's session cookie.

    Falls back to ``ctx.guild_id`` for OpenAuth (LAN mode) or sessions
    created before multi-guild support was added.
    """
    from web.auth import SESSION_COOKIE, DiscordOAuthAuth

    auth = request.app.state.auth
    if not isinstance(auth, DiscordOAuthAuth):
        return request.app.state.ctx.guild_id

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session = auth.read_session(cookie)
        if session and "guild_id" in session:
            return int(session["guild_id"])
    return request.app.state.ctx.guild_id


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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
            )
        if not required_frozen.issubset(user.perms):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
            )
        return user

    return _dep
