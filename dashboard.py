"""Standalone dashboard launcher.

Runs only the FastAPI dashboard against the populated SQLite database —
no Discord connection, no bot, no outbound network. Useful for local dev:

    python dashboard.py

Environment:
    DASHBOARD_HOST   (default 127.0.0.1)
    DASHBOARD_PORT   (default 8080)
    GUILD_ID         (fallback if config table has no guild_id row)
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

from db_utils import get_config_value, open_db
from services.message_store import init_known_channels_table, init_known_users_table

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dungeonkeeper.dashboard")


class _NullBot:
    """Stand-in for ``discord.Client`` used in standalone mode."""

    def get_guild(self, _guild_id):  # noqa: D401 - interface stub
        return None


@dataclass
class StandaloneContext:
    """Minimal duck-typed context providing what the web routes actually read.

    The web routes only touch ``ctx.open_db()``, ``ctx.guild_id``, and
    ``ctx.bot.get_guild(...)``. Everything else in ``AppContext`` (grant role
    config, XP state, watched users, etc.) is unused by the dashboard and is
    not constructed here.
    """

    db_path: Path
    guild_id: int
    bot: _NullBot
    tz_offset_hours: float = 0.0
    welcome_channel_id: int = 0
    greeter_role_id: int = 0

    def open_db(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        return open_db(self.db_path)


def _resolve_guild_id(db_path: Path) -> int:
    with open_db(db_path) as conn:
        raw = get_config_value(conn, "guild_id", "0")
    try:
        gid = int(raw)
    except (TypeError, ValueError):
        gid = 0
    if gid == 0:
        gid = int(os.environ.get("GUILD_ID", "0") or 0)
    return gid


def main() -> None:
    load_dotenv()

    db_path = Path(__file__).with_name("dungeonkeeper.db")
    if not db_path.exists():
        raise SystemExit(f"Database not found at {db_path}")

    guild_id = _resolve_guild_id(db_path)
    if guild_id == 0:
        log.warning("No guild_id configured; role-growth and other queries will return empty.")

    tz_offset = 0.0
    with open_db(db_path) as conn:
        raw_tz = get_config_value(conn, "tz_offset_hours", "0")
        try:
            tz_offset = float(raw_tz)
        except (TypeError, ValueError):
            pass

    # Ensure lookup tables exist (may not if bot hasn't run yet)
    with open_db(db_path) as conn:
        init_known_users_table(conn)
        init_known_channels_table(conn)

    welcome_channel_id = 0
    greeter_role_id = 0
    with open_db(db_path) as conn:
        try:
            welcome_channel_id = int(get_config_value(conn, "welcome_channel_id", "0"))
        except (TypeError, ValueError):
            pass
        try:
            greeter_role_id = int(get_config_value(conn, "greeter_role_id", "0"))
        except (TypeError, ValueError):
            pass

    ctx = StandaloneContext(
        db_path=db_path, guild_id=guild_id, bot=_NullBot(),
        tz_offset_hours=tz_offset,
        welcome_channel_id=welcome_channel_id,
        greeter_role_id=greeter_role_id,
    )

    # Import after env is loaded so matplotlib etc. can honour any configuration.
    from web.server import create_app

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8080"))

    app = create_app(ctx)
    log.info("Standalone dashboard starting on http://%s:%d (db=%s, guild=%d)",
             host, port, db_path, guild_id)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
