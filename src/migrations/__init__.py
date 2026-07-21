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


def _strip_line_comments(sql: str) -> str:
    """Remove -- comments from SQL, respecting single-quoted string literals."""
    lines = []
    for line in sql.splitlines():
        in_string = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == "'" and not in_string:
                in_string = True
            elif c == "'" and in_string:
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 1  # escaped quote
                else:
                    in_string = False
            elif not in_string and line[i : i + 2] == "--":
                line = line[:i]
                break
            i += 1
        lines.append(line)
    return "\n".join(lines)


def _exec_migration_sql(conn: sqlite3.Connection, sql: str, name: str) -> None:
    """Run each statement in a migration individually, tolerating already-applied DDL.

    Deliberately does NOT commit — the caller commits the statements together
    with the schema_version insert so each migration applies atomically (a
    crash mid-migration rolls back instead of re-running half-applied DDL).
    """
    for stmt in _strip_line_comments(sql).split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc):
                log.warning("Already-applied DDL skipped in %s: %s", name, exc)
            else:
                raise


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
            try:
                # Explicit BEGIN: in the sqlite3 module's legacy isolation mode
                # DDL runs in autocommit (no implicit transaction), so without
                # this a failed migration could leave earlier statements applied.
                conn.execute("BEGIN")
                _exec_migration_sql(conn, sql, name)
                conn.execute(
                    "INSERT INTO schema_version (migration, applied_at) VALUES (?, ?)",
                    (name, time.time()),
                )
                conn.commit()  # statements + version row land atomically
            except Exception:
                conn.rollback()
                raise
            log.info("Applied migration: %s", name)


async def _exec_migration_sql_async(db, sql: str, name: str) -> None:
    """Run each statement in a migration individually, tolerating already-applied DDL.

    Deliberately does NOT commit — see _exec_migration_sql.
    """
    for stmt in _strip_line_comments(sql).split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            await db.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc):
                log.warning("Already-applied DDL skipped in %s: %s", name, exc)
            else:
                raise


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
        try:
            # Explicit BEGIN — see apply_migrations_sync for why.
            await db.execute("BEGIN")
            await _exec_migration_sql_async(db, sql, name)
            await db.execute(
                "INSERT INTO schema_version (migration, applied_at) VALUES (?, ?)",
                (name, time.time()),
            )
            await db.commit()  # statements + version row land atomically
        except Exception:
            await db.rollback()
            raise
        log.info("Applied migration: %s", name)
