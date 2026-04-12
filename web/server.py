"""FastAPI app factory and uvicorn launcher for the dashboard.

The launcher coroutine ``serve_forever`` is designed to be appended to
``bot.startup_task_factories`` in ``dungeonkeeper.py``. It shares the bot's
event loop, reuses ``AppContext`` / ``ctx.open_db()``, and reads live
``ctx.bot.get_guild(...)`` data.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from services.health_service import init_health_tables
from services.moderation import init_moderation_tables
from services.wellness_service import init_wellness_tables
from web.auth import AuthBackend, DiscordOAuthAuth, OpenAuth

_STATIC_DIR = Path(__file__).parent / "static"
_log = logging.getLogger("dungeonkeeper.web")


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

    _log.info("Open authentication (LAN mode) — set DISCORD_CLIENT_ID + SESSION_SECRET to enable OAuth")
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
    app.include_router(ai_config_routes.router, prefix="/api/config", tags=["ai-config"])
    app.include_router(messages_routes.router, prefix="/api", tags=["messages"])
    app.include_router(moderation_routes.router, prefix="/api", tags=["moderation"])
    app.include_router(logs_routes.router, prefix="/api", tags=["logs"])

    # ── Health dashboard routes ────────────────────────────────────
    from web.routes import health as health_routes

    app.include_router(health_routes.router, prefix="/api", tags=["health"])

    # ── Wellness routes ─────────────────────────────────────────────
    from web.wellness_routes import api as wellness_api
    from web.wellness_routes import admin as wellness_admin

    app.include_router(wellness_api.router, prefix="/api/wellness", tags=["wellness"])
    app.include_router(wellness_admin.router, prefix="/api/wellness/admin", tags=["wellness-admin"])

    # Install the log handler so records flow to the SSE stream
    logs_routes.install_log_handler()

    # ── Static files & HTML entry points ────────────────────────────
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/login", include_in_schema=False)
        async def _login():
            return FileResponse(str(_STATIC_DIR / "login.html"))

        @app.get("/", include_in_schema=False)
        async def _index(request: Request):
            # When OAuth is active, require a valid session to view the dashboard
            if isinstance(app.state.auth, DiscordOAuthAuth):
                user = await app.state.auth.authenticate(request)
                if user is None:
                    return RedirectResponse("/login", status_code=302)
            return FileResponse(str(_STATIC_DIR / "index.html"))

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
