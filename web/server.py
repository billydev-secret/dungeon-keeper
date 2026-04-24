"""FastAPI app factory and uvicorn launcher for the dashboard.

The launcher coroutine ``serve_forever`` is designed to be appended to
``bot.startup_task_factories`` in ``dungeonkeeper.py``. It shares the bot's
event loop, reuses ``AppContext`` / ``ctx.open_db()``, and reads live
``ctx.bot.get_guild(...)`` data.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from services.auto_delete_service import init_auto_delete_tables
from services.booster_roles import init_booster_role_tables
from services.confessions_service import init_db as init_confessions_db
from services.dm_perms_service import init_db as init_dm_perms_db
from services.health_service import init_health_tables
from services.moderation import init_moderation_tables
from services.wellness_service import init_wellness_tables
from web.auth import AuthBackend, DiscordOAuthAuth, OpenAuth

_STATIC_DIR = Path(__file__).parent / "static"
_log = logging.getLogger("dungeonkeeper.web")

# Per-boot cache-buster. Every module import in served JS, and every ?v= in
# served HTML, gets rewritten to carry this token so browsers (and Cloudflare)
# treat each reboot as a fresh URL set.
_BOOT_ID = str(int(time.time()))

# Matches `from "./x.js"`, `from '../x.js'`, `import("./x.js")`, etc.
# Only relative specifiers (./ or ../) so we don't touch bare imports or URLs.
_JS_IMPORT_RE = re.compile(
    r'''((?:\bfrom|\bimport)\s*\(?\s*["'])(\.{1,2}/[^"']+\.js)(["'])'''
)


@lru_cache(maxsize=4)
def _html_with_boot_id(name: str) -> str:
    text = (_STATIC_DIR / name).read_text(encoding="utf-8")
    return re.sub(r"\?v=\d+", f"?v={_BOOT_ID}", text)


# ── Rate limiting ────────────────────────────────────────────────────
# Simple per-IP token bucket. No external dependency needed.

# Tier definitions: (max_tokens, refill_per_second)
_RATE_TIERS: dict[str, tuple[int, float]] = {
    "ai":      (5, 0.1),     # 5 burst, 1 per 10s — expensive AI calls
    "search":  (10, 0.5),    # 10 burst, 1 per 2s — regex search
    "auth":    (10, 0.2),    # 10 burst, 1 per 5s — login/callback
    "default": (60, 2.0),    # 60 burst, 2/s — normal API
}

# Map path prefixes to tiers
_TIER_ROUTES: list[tuple[str, str]] = [
    ("/api/messages/ai-query", "ai"),
    ("/api/messages/search",   "search"),
    ("/login",                 "auth"),
    ("/callback",              "auth"),
]


class _RateBucket:
    __slots__ = ("tokens", "last_refill", "max_tokens", "refill_rate")

    def __init__(self, max_tokens: float, refill_rate: float) -> None:
        self.tokens = max_tokens
        self.last_refill = time.monotonic()
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate

    def consume(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ip → tier → bucket
_buckets: dict[str, dict[str, _RateBucket]] = defaultdict(dict)
_BUCKET_EXPIRY = 600  # prune IPs not seen in 10 minutes
_last_prune = time.monotonic()


def _get_tier(path: str) -> str:
    for prefix, tier in _TIER_ROUTES:
        if path.startswith(prefix):
            return tier
    return "default"


class _RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global _last_prune  # noqa: PLW0603

        path = request.url.path
        # Skip static files — no rate limit needed
        if path.startswith("/static/"):
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        tier = _get_tier(path)
        max_tokens, refill_rate = _RATE_TIERS[tier]

        ip_buckets = _buckets[ip]
        bucket = ip_buckets.get(tier)
        if bucket is None:
            bucket = _RateBucket(max_tokens, refill_rate)
            ip_buckets[tier] = bucket

        if not bucket.consume():
            _log.warning("Rate limited %s on %s (tier=%s)", ip, path, tier)
            return JSONResponse(
                {"detail": "Too many requests. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(int(1 / refill_rate))},
            )

        # Periodic prune of stale buckets (every 10 min)
        now = time.monotonic()
        if now - _last_prune > _BUCKET_EXPIRY:
            _last_prune = now
            stale = [
                k for k, v in _buckets.items()
                if all(now - b.last_refill > _BUCKET_EXPIRY for b in v.values())
            ]
            for k in stale:
                del _buckets[k]

        return await call_next(request)


def _auto_detect_auth(guild_id: int) -> AuthBackend:
    """Pick an auth backend based on environment variables.

    If DISCORD_CLIENT_ID and SESSION_SECRET are set, use Discord OAuth2.
    Otherwise fall back to the open (LAN-only) backend.
    """
    client_id = os.getenv("DISCORD_CLIENT_ID")
    session_secret = os.getenv("SESSION_SECRET")

    if client_id and session_secret:
        _log.info("Discord OAuth2 authentication enabled (client_id=%s)", client_id)
        return DiscordOAuthAuth(session_secret, guild_id)

    _log.info(
        "Open authentication (LAN mode) — set DISCORD_CLIENT_ID + SESSION_SECRET to enable OAuth"
    )
    return OpenAuth()


def create_app(ctx, auth: AuthBackend | None = None) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="TGM Dashboard", docs_url="/api/docs", redoc_url=None)
    app.state.ctx = ctx
    app.state.auth = auth or _auto_detect_auth(ctx.guild_id)

    # Ensure tables exist (in case bot hasn't restarted yet)
    with ctx.open_db() as conn:
        init_moderation_tables(conn)
        init_wellness_tables(conn)
        init_health_tables(conn)
        init_booster_role_tables(conn)
        init_auto_delete_tables(conn)
    init_confessions_db(ctx.db_path)
    init_dm_perms_db(ctx.db_path)

    # ── OAuth routes (login / callback / logout) ────────────────────
    from web.routes import oauth as oauth_routes

    app.include_router(oauth_routes.router, tags=["auth"])

    # ── API routes ──────────────────────────────────────────────────
    from web.routes import ai_config as ai_config_routes
    from web.routes import config as config_routes
    from web.routes import home as home_routes
    from web.routes import logs as logs_routes
    from web.routes import messages as messages_routes
    from web.routes import meta as meta_routes
    from web.routes import moderation as moderation_routes
    from web.routes import reports as reports_routes

    app.include_router(home_routes.router, prefix="/api", tags=["home"])
    app.include_router(meta_routes.router, prefix="/api", tags=["meta"])
    app.include_router(reports_routes.router, prefix="/api/reports", tags=["reports"])
    app.include_router(config_routes.router, prefix="/api", tags=["config"])
    app.include_router(
        ai_config_routes.router, prefix="/api/config", tags=["ai-config"]
    )
    app.include_router(messages_routes.router, prefix="/api", tags=["messages"])
    app.include_router(moderation_routes.router, prefix="/api", tags=["moderation"])
    app.include_router(logs_routes.router, prefix="/api", tags=["logs"])

    from web.routes import todo as todo_routes

    app.include_router(todo_routes.router, prefix="/api", tags=["todos"])

    # ── Health dashboard routes ────────────────────────────────────
    from web.routes import health as health_routes

    app.include_router(health_routes.router, prefix="/api", tags=["health"])

    # ── Wellness routes ─────────────────────────────────────────────
    from web.wellness_routes import admin as wellness_admin
    from web.wellness_routes import api as wellness_api

    app.include_router(wellness_api.router, prefix="/api/wellness", tags=["wellness"])
    app.include_router(
        wellness_admin.router, prefix="/api/wellness/admin", tags=["wellness-admin"]
    )

    # Install the log handler so records flow to the SSE stream
    logs_routes.install_log_handler()

    # ── Rate limiting ──────────────────────────────────────────────────
    app.add_middleware(_RateLimitMiddleware)

    # ── Per-boot cache-busting for JS/CSS ──────────────────────────────
    # JS responses get their relative imports rewritten to `...?v={BOOT_ID}`,
    # which cascades through the whole module graph. Content is then safe to
    # cache long because a new boot → new URLs. CSS gets the same treatment
    # via the HTML ?v= token (rewritten in _html_with_boot_id).
    class _CacheBustJS(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            path = request.url.path
            if not (path.startswith("/static/") and path.endswith(".js")):
                return response
            raw_chunks: list[Any] = []
            async for chunk in cast(StreamingResponse, response).body_iterator:
                raw_chunks.append(chunk)
            body = b"".join(
                c if isinstance(c, bytes) else bytes(c) if isinstance(c, memoryview) else c.encode()
                for c in raw_chunks
            )
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )
            text = _JS_IMPORT_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}?v={_BOOT_ID}{m.group(3)}",
                text,
            )
            # Also rewrite any standalone ?v=NN tokens (e.g. hardcoded
            # dynamic-import cache-bust constants) so every per-boot URL
            # changes, forcing the browser to refetch the module graph.
            new_body = re.sub(r"\?v=\d+", f"?v={_BOOT_ID}", text).encode("utf-8")
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers["cache-control"] = "public, max-age=31536000, immutable"
            return Response(
                content=new_body,
                status_code=response.status_code,
                headers=headers,
                media_type="application/javascript",
            )

    app.add_middleware(_CacheBustJS)

    # ── Static files & HTML entry points ────────────────────────────
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/login", include_in_schema=False)
        async def _login():
            return HTMLResponse(
                content=_html_with_boot_id("login.html"),
                headers={"cache-control": "no-cache"},
            )

        @app.get("/", include_in_schema=False)
        async def _index(request: Request):
            # When OAuth is active, require a valid session to view the dashboard
            if isinstance(app.state.auth, DiscordOAuthAuth):
                user = await app.state.auth.authenticate(request)
                if user is None:
                    return RedirectResponse("/login", status_code=302)
            return HTMLResponse(
                content=_html_with_boot_id("index.html"),
                headers={"cache-control": "no-cache"},
            )

    return app


async def serve_forever(ctx, host: str, port: int) -> None:  # noqa: ANN001
    """Run the FastAPI app under uvicorn on the current event loop."""
    app = create_app(ctx)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        loop="asyncio",
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    _log.info("Dashboard starting on http://%s:%d", host, port)
    try:
        await server.serve()
    except Exception:
        _log.exception("Dashboard server crashed")
