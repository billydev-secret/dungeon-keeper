"""Gender classification service — mods assign gender to members for NSFW analytics."""
from __future__ import annotations

import sqlite3
import time

VALID_GENDERS = ("male", "female", "nonbinary")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_gender_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_gender (
            guild_id  INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            gender    TEXT NOT NULL,
            set_by    INTEGER NOT NULL,
            set_at    REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


def set_gender(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    gender: str,
    set_by: int,
    set_at: float | None = None,
) -> None:
    if set_at is None:
        set_at = time.time()
    conn.execute(
        """
        INSERT INTO member_gender (guild_id, user_id, gender, set_by, set_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            gender = excluded.gender,
            set_by = excluded.set_by,
            set_at = excluded.set_at
        """,
        (guild_id, user_id, gender, set_by, set_at),
    )


def get_gender(conn: sqlite3.Connection, guild_id: int, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT gender FROM member_gender WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return str(row["gender"]) if row else None


def get_gender_map(
    conn: sqlite3.Connection, guild_id: int, user_ids: list[int],
) -> dict[int, str]:
    if not user_ids:
        return {}
    result: dict[int, str] = {}
    batch_size = 800
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i:i + batch_size]
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT user_id, gender FROM member_gender "
            f"WHERE guild_id = ? AND user_id IN ({placeholders})",
            [guild_id, *batch],
        ).fetchall()
        for row in rows:
            result[int(row["user_id"])] = str(row["gender"])
    return result


def get_unclassified_member_ids(
    conn: sqlite3.Connection, guild_id: int, all_member_ids: list[int],
) -> list[int]:
    if not all_member_ids:
        return []
    classified: set[int] = set()
    batch_size = 800
    for i in range(0, len(all_member_ids), batch_size):
        batch = all_member_ids[i:i + batch_size]
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT user_id FROM member_gender "
            f"WHERE guild_id = ? AND user_id IN ({placeholders})",
            [guild_id, *batch],
        ).fetchall()
        classified.update(int(row["user_id"]) for row in rows)
    return [uid for uid in all_member_ids if uid not in classified]
