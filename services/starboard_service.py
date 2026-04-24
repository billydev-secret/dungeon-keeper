"""Starboard service — DB layer for the starboard feature."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional


def get_starboard_config(conn: sqlite3.Connection, guild_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT channel_id, threshold, emoji, enabled FROM starboard_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()


def upsert_starboard_config(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_id: int,
    threshold: int,
    emoji: str,
    enabled: int,
) -> None:
    conn.execute(
        """
        INSERT INTO starboard_config (guild_id, channel_id, threshold, emoji, enabled)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            channel_id = excluded.channel_id,
            threshold  = excluded.threshold,
            emoji      = excluded.emoji,
            enabled    = excluded.enabled
        """,
        (guild_id, channel_id, threshold, emoji, enabled),
    )


def get_starboard_post(
    conn: sqlite3.Connection, guild_id: int, original_message_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT original_message_id, starboard_message_id, original_channel_id,
               author_id, star_count
        FROM starboard_posts
        WHERE guild_id = ? AND original_message_id = ?
        """,
        (guild_id, original_message_id),
    ).fetchone()


def insert_starboard_post(
    conn: sqlite3.Connection,
    guild_id: int,
    original_message_id: int,
    starboard_message_id: int,
    original_channel_id: int,
    author_id: int,
    star_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO starboard_posts
            (guild_id, original_message_id, starboard_message_id,
             original_channel_id, author_id, star_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            original_message_id,
            starboard_message_id,
            original_channel_id,
            author_id,
            star_count,
            time.time(),
        ),
    )


def update_starboard_post_count(
    conn: sqlite3.Connection, guild_id: int, original_message_id: int, star_count: int
) -> None:
    conn.execute(
        "UPDATE starboard_posts SET star_count = ? WHERE guild_id = ? AND original_message_id = ?",
        (star_count, guild_id, original_message_id),
    )


def add_reactor(
    conn: sqlite3.Connection, guild_id: int, message_id: int, user_id: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO starboard_reactors (guild_id, message_id, user_id) VALUES (?, ?, ?)",
        (guild_id, message_id, user_id),
    )


def remove_reactor(
    conn: sqlite3.Connection, guild_id: int, message_id: int, user_id: int
) -> None:
    conn.execute(
        "DELETE FROM starboard_reactors WHERE guild_id = ? AND message_id = ? AND user_id = ?",
        (guild_id, message_id, user_id),
    )


def get_effective_star_count(
    conn: sqlite3.Connection, guild_id: int, message_id: int, author_id: int
) -> int:
    """Count star reactors, excluding the message author (no self-stars)."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM starboard_reactors WHERE guild_id = ? AND message_id = ? AND user_id != ?",
        (guild_id, message_id, author_id),
    ).fetchone()
    return int(row["cnt"]) if row else 0
