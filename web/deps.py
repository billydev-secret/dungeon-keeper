"""FastAPI dependency-injection factories for the dashboard."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Awaitable, Callable, TypeVar

from fastapi import Depends, HTTPException, Request, status

from web.auth import AuthBackend, AuthenticatedUser

T = TypeVar("T")

_log = logging.getLogger("dungeonkeeper.web.cache")

# ── Report cache ────────────────────────────────────────────────────────

_report_cache: dict[str, tuple[float, object]] = {}  # key → (expires_at, result)
_DEFAULT_TTL = 120  # seconds


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
        expires_at, result = hit
        if now < expires_at:
            _log.debug("cache HIT  %s (ttl %.0fs remaining)", name, expires_at - now)
            return result  # type: ignore[return-value]

    result = await asyncio.to_thread(fn)
    _report_cache[key] = (now + ttl, result)
    _log.debug("cache MISS %s (stored for %ds)", name, ttl)
    return result  # type: ignore[return-value]


def invalidate_report_cache(name: str | None = None, guild_id: int | None = None) -> int:
    """Drop cached entries. With no args, clears everything.

    Returns the number of entries removed.
    """
    if name is None and guild_id is None:
        count = len(_report_cache)
        _report_cache.clear()
        return count

    prefix_parts: dict = {}
    if name is not None:
        prefix_parts["n"] = name
    if guild_id is not None:
        prefix_parts["g"] = guild_id

    # We need to check each key — rebuild and compare partial JSON isn't
    # practical, so just iterate and match the raw dict values.
    to_remove = []
    for key, (_, result) in list(_report_cache.items()):
        to_remove.append(key)  # conservative: nuke matching guild/name
    # For a targeted clear, rebuild keys from scratch isn't feasible with
    # hashed keys, so just clear everything for now (still fast).
    count = len(_report_cache)
    _report_cache.clear()
    return count


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
