from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import TypedDict


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse string to boolean value."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@contextlib.contextmanager
def open_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection, yield it inside a transaction, then close.

    Uses deferred transactions so ``with open_db(...) as conn:`` commits
    on success and rolls back on exception.
    """
    conn = sqlite3.connect(db_path, timeout=30, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with conn:  # BEGIN … COMMIT/ROLLBACK
            yield conn
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# Grant roles
# ---------------------------------------------------------------------------

class GrantRoleConfig(TypedDict):
    grant_name: str
    label: str
    role_id: int
    log_channel_id: int
    announce_channel_id: int
    grant_message: str


_DEFAULT_GRANT_ROLES: list[tuple[str, str]] = [
    ("denizen", "Denizen"),
    ("nsfw", "NSFW"),
    ("veteran", "Veteran"),
    ("kink", "Kink"),
    ("goldengirl", "Golden Girl"),
]


def init_grant_role_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grant_roles (
            guild_id            INTEGER NOT NULL,
            grant_name          TEXT NOT NULL,
            label               TEXT NOT NULL DEFAULT '',
            role_id             INTEGER NOT NULL DEFAULT 0,
            log_channel_id      INTEGER NOT NULL DEFAULT 0,
            announce_channel_id INTEGER NOT NULL DEFAULT 0,
            grant_message       TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (guild_id, grant_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grant_role_permissions (
            guild_id    INTEGER NOT NULL,
            grant_name  TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('user', 'role')),
            entity_id   INTEGER NOT NULL,
            PRIMARY KEY (guild_id, grant_name, entity_type, entity_id)
        )
        """
    )


def migrate_grant_roles(conn: sqlite3.Connection, guild_id: int) -> None:
    """One-time migration: copy legacy config keys into grant_roles table."""
    existing = conn.execute(
        "SELECT 1 FROM grant_roles WHERE guild_id = ? LIMIT 1", (guild_id,)
    ).fetchone()
    if existing:
        return

    for grant_name, label in _DEFAULT_GRANT_ROLES:
        role_id = int(get_config_value(conn, f"{grant_name}_role_id", "0") or 0)
        log_ch = int(get_config_value(conn, f"{grant_name}_log_channel_id", "0") or 0)
        ann_ch = int(get_config_value(conn, f"{grant_name}_announce_channel_id", "0") or 0)
        msg = get_config_value(conn, f"{grant_name}_grant_message", "")
        conn.execute(
            """
            INSERT OR IGNORE INTO grant_roles
                (guild_id, grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, grant_name, label, role_id, log_ch, ann_ch, msg),
        )

    # Migrate greeter role as a permission for all grant roles
    greeter_id = int(get_config_value(conn, "greeter_role_id", "0") or 0)
    if greeter_id > 0:
        for grant_name, _ in _DEFAULT_GRANT_ROLES:
            conn.execute(
                "INSERT OR IGNORE INTO grant_role_permissions (guild_id, grant_name, entity_type, entity_id) VALUES (?, ?, 'role', ?)",
                (guild_id, grant_name, greeter_id),
            )


def get_grant_roles(conn: sqlite3.Connection, guild_id: int) -> dict[str, GrantRoleConfig]:
    rows = conn.execute(
        "SELECT grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message "
        "FROM grant_roles WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {
        row["grant_name"]: GrantRoleConfig(
            grant_name=row["grant_name"],
            label=row["label"],
            role_id=row["role_id"],
            log_channel_id=row["log_channel_id"],
            announce_channel_id=row["announce_channel_id"],
            grant_message=row["grant_message"],
        )
        for row in rows
    }


def upsert_grant_role(
    conn: sqlite3.Connection,
    guild_id: int,
    grant_name: str,
    *,
    label: str,
    role_id: int,
    log_channel_id: int,
    announce_channel_id: int,
    grant_message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO grant_roles
            (guild_id, grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, grant_name) DO UPDATE SET
            label=excluded.label, role_id=excluded.role_id,
            log_channel_id=excluded.log_channel_id,
            announce_channel_id=excluded.announce_channel_id,
            grant_message=excluded.grant_message
        """,
        (guild_id, grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message),
    )


def delete_grant_role(conn: sqlite3.Connection, guild_id: int, grant_name: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM grant_roles WHERE guild_id = ? AND grant_name = ?",
        (guild_id, grant_name),
    )
    conn.execute(
        "DELETE FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ?",
        (guild_id, grant_name),
    )
    return cursor.rowcount > 0


def get_grant_permissions(
    conn: sqlite3.Connection, guild_id: int, grant_name: str,
) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT entity_type, entity_id FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ?",
        (guild_id, grant_name),
    ).fetchall()
    return [(row["entity_type"], row["entity_id"]) for row in rows]


def add_grant_permission(
    conn: sqlite3.Connection, guild_id: int, grant_name: str, entity_type: str, entity_id: int,
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO grant_role_permissions (guild_id, grant_name, entity_type, entity_id) VALUES (?, ?, ?, ?)",
        (guild_id, grant_name, entity_type, entity_id),
    )
    return (cur.rowcount or 0) > 0


def remove_grant_permission(
    conn: sqlite3.Connection, guild_id: int, grant_name: str, entity_type: str, entity_id: int,
) -> bool:
    cur = conn.execute(
        "DELETE FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ? AND entity_type = ? AND entity_id = ?",
        (guild_id, grant_name, entity_type, entity_id),
    )
    return (cur.rowcount or 0) > 0


def can_use_grant(
    conn: sqlite3.Connection, guild_id: int, grant_name: str, member_id: int, role_ids: list[int],
) -> bool:
    """Check if a member (by ID and their role IDs) has permission for a grant role."""
    # Check user permission
    row = conn.execute(
        "SELECT 1 FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ? AND entity_type = 'user' AND entity_id = ?",
        (guild_id, grant_name, member_id),
    ).fetchone()
    if row:
        return True
    # Check role permissions
    if role_ids:
        placeholders = ",".join("?" for _ in role_ids)
        row = conn.execute(
            f"SELECT 1 FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ? AND entity_type = 'role' AND entity_id IN ({placeholders})",
            [guild_id, grant_name, *role_ids],
        ).fetchone()
        if row:
            return True
    return False


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
