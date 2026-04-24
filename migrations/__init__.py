"""Versioned SQL migration framework for Dungeon Keeper.

Migrations are plain .sql files named NNN_description.sql in this directory.
The schema_version table tracks which have been applied; each is idempotent.

Sync entry point (used at bot startup):
    from migrations import apply_migrations_sync
    apply_migrations_sync(db_path)

Async entry point (used in tests with aiosqlite):
    from migrations import apply_migrations
    await apply_migrations(db)
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("dungeonkeeper.migrations")

_MIGRATIONS_DIR = Path(__file__).parent

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    migration TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
)
"""


def _migration_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def apply_migrations_sync(db_path: str | Path) -> None:
    """Apply all pending migrations to the database at db_path (sync/sqlite3)."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(_CREATE_SCHEMA_VERSION)
        conn.commit()

        applied = {row["migration"] for row in conn.execute("SELECT migration FROM schema_version")}

        for path in _migration_files():
            name = path.name
            if name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            log.info("Applying migration: %s", name)
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (migration, applied_at) VALUES (?, ?)",
                (name, time.time()),
            )
            conn.commit()
            log.info("Applied migration: %s", name)


async def apply_migrations(db) -> None:
    """Apply all pending migrations to an open aiosqlite connection (async)."""
    await db.execute(_CREATE_SCHEMA_VERSION)
    await db.commit()

    applied = {
        row[0]
        async for row in await db.execute("SELECT migration FROM schema_version")
    }

    for path in _migration_files():
        name = path.name
        if name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("Applying migration: %s", name)
        await db.executescript(sql)
        await db.execute(
            "INSERT INTO schema_version (migration, applied_at) VALUES (?, ?)",
            (name, time.time()),
        )
        await db.commit()
        log.info("Applied migration: %s", name)
