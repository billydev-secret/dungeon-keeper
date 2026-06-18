"""Birthday tracker — DB helpers extracted from birthday_cog for testability."""

from __future__ import annotations

import sqlite3
import time

# Max valid day per month; Feb capped at 28 (Feb 29 skips 3/4 years)
MAX_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def upsert_birthday(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    month: int,
    day: int,
    set_by: int,
    preference: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO member_birthdays (guild_id, user_id, birth_month, birth_day, set_by, set_at, preference)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            birth_month = excluded.birth_month,
            birth_day   = excluded.birth_day,
            set_by      = excluded.set_by,
            set_at      = excluded.set_at,
            preference  = excluded.preference
        """,
        (guild_id, user_id, month, day, set_by, time.time(), preference),
    )


def delete_birthday(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> bool:
    cur = conn.execute(
        "DELETE FROM member_birthdays WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def list_all_birthdays(
    conn: sqlite3.Connection, guild_id: int
) -> list[tuple[int, int, int, str | None]]:
    rows = conn.execute(
        "SELECT user_id, birth_month, birth_day, preference FROM member_birthdays "
        "WHERE guild_id = ? ORDER BY birth_month, birth_day",
        (guild_id,),
    ).fetchall()
    return [
        (row["user_id"], row["birth_month"], row["birth_day"], row["preference"])
        for row in rows
    ]


def get_birthday_preference(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> str | None:
    row = conn.execute(
        "SELECT preference FROM member_birthdays WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return row["preference"] if row else None


def todays_unannounced(
    conn: sqlite3.Connection,
    guild_id: int,
    month: int,
    day: int,
    date_iso: str,
) -> list[int]:
    """Return user_ids whose birthday is today and haven't been announced yet."""
    rows = conn.execute(
        """
        SELECT b.user_id
        FROM member_birthdays b
        LEFT JOIN birthday_announcements a
            ON a.guild_id = b.guild_id AND a.user_id = b.user_id AND a.announced_date = ?
        WHERE b.guild_id = ? AND b.birth_month = ? AND b.birth_day = ? AND a.user_id IS NULL
        """,
        (date_iso, guild_id, month, day),
    ).fetchall()
    return [row["user_id"] for row in rows]


def mark_announced(
    conn: sqlite3.Connection, guild_id: int, user_id: int, date_iso: str
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO birthday_announcements (guild_id, user_id, announced_date) VALUES (?, ?, ?)",
        (guild_id, user_id, date_iso),
    )
    return (cur.rowcount or 0) > 0


# ── Pin tracking ───────────────────────────────────────────────────────


def record_pin(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    date_iso: str,
) -> None:
    """Remember a pinned birthday message so a later pass can unpin it."""
    conn.execute(
        "INSERT OR REPLACE INTO birthday_pins "
        "(guild_id, channel_id, message_id, pinned_date) VALUES (?, ?, ?, ?)",
        (guild_id, channel_id, message_id, date_iso),
    )


def pins_before(
    conn: sqlite3.Connection, guild_id: int, before_iso: str
) -> list[tuple[int, int]]:
    """Return (channel_id, message_id) of pins recorded before ``before_iso``."""
    rows = conn.execute(
        "SELECT channel_id, message_id FROM birthday_pins "
        "WHERE guild_id = ? AND pinned_date < ?",
        (guild_id, before_iso),
    ).fetchall()
    return [(row["channel_id"], row["message_id"]) for row in rows]


def clear_pin(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, message_id: int
) -> None:
    """Drop a tracked pin row once the message has been unpinned (or is gone)."""
    conn.execute(
        "DELETE FROM birthday_pins "
        "WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
        (guild_id, channel_id, message_id),
    )
