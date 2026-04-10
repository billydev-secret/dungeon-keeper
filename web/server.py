"""FastAPI app factory and uvicorn launcher for the dashboard.

The launcher coroutine ``serve_forever`` is designed to be appended to
``bot.startup_task_factories`` in ``dungeonkeeper.py``. It shares the bot's
event loop, reuses ``AppContext`` / ``ctx.open_db()``, and reads live
``ctx.bot.get_guild(...)`` data.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from services.moderation import init_moderation_tables
from web.auth import AuthBackend, OpenAuth
from web.routes import config as config_routes
from web.routes import home as home_routes
from web.routes import messages as messages_routes
from web.routes import meta as meta_routes
from web.routes import moderation as moderation_routes
from web.routes import reports as reports_routes

_STATIC_DIR = Path(__file__).parent / "static"
_log = logging.getLogger("dungeonkeeper.web")


def create_app(ctx, auth: AuthBackend | None = None) -> FastAPI:
    app = FastAPI(title="Dungeonkeeper Dashboard", docs_url="/api/docs", redoc_url=None)
    app.state.ctx = ctx
    app.state.auth = auth or OpenAuth()

    # Ensure moderation tables exist (in case bot hasn't restarted yet)
    with ctx.open_db() as conn:
        init_moderation_tables(conn)

    app.include_router(home_routes.router, prefix="/api", tags=["home"])
    app.include_router(meta_routes.router, prefix="/api", tags=["meta"])
    app.include_router(reports_routes.router, prefix="/api/reports", tags=["reports"])
    app.include_router(config_routes.router, prefix="/api", tags=["config"])
    app.include_router(messages_routes.router, prefix="/api", tags=["messages"])
    app.include_router(moderation_routes.router, prefix="/api", tags=["moderation"])

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def _index():
            return FileResponse(str(_STATIC_DIR / "index.html"))

    return app


async def serve_forever(ctx, host: str, port: int) -> None:
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
