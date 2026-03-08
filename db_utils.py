from __future__ import annotations

import sqlite3
from pathlib import Path


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse string to boolean value."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open SQLite database connection with WAL mode and timeout settings."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_config_value(conn: sqlite3.Connection, key: str, default: str) -> str:
    """Retrieve configuration value from config table."""
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_config_id_set(conn: sqlite3.Connection, bucket: str) -> set[int]:
    """Retrieve set of integer IDs from config_ids table for given bucket."""
    rows = conn.execute(
        "SELECT value FROM config_ids WHERE bucket = ? ORDER BY value",
        (bucket,),
    ).fetchall()
    return {int(row["value"]) for row in rows}


def init_config_db(db_path: Path) -> None:
    """Initialize configuration database tables."""
    with open_db(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config_ids (
                bucket TEXT NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (bucket, value)
            )
            """
        )
