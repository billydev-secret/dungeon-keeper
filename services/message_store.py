"""Message content archive.

Stores message text, reply references, attachment URLs, @mentions, and
reaction counts so they can be queried by other services (AI review, etc.).

All writes are idempotent — safe to call from both the live event handler
and the /interaction_scan backfill without creating duplicates.
"""
from __future__ import annotations

import sqlite3


def init_message_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id  INTEGER PRIMARY KEY,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            author_id   INTEGER NOT NULL,
            content     TEXT,
            reply_to_id INTEGER,
            ts          INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_guild_ts "
        "ON messages (guild_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_author "
        "ON messages (guild_id, author_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_attachments (
            message_id  INTEGER NOT NULL,
            url         TEXT NOT NULL,
            PRIMARY KEY (message_id, url)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_mentions (
            message_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mentions_user "
        "ON message_mentions (user_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_reactions (
            message_id  INTEGER NOT NULL,
            emoji       TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (message_id, emoji)
        )
        """
    )


def store_message(
    conn: sqlite3.Connection,
    message_id: int,
    guild_id: int,
    channel_id: int,
    author_id: int,
    content: str | None,
    reply_to_id: int | None,
    ts: int,
    attachment_urls: list[str],
    mention_ids: list[int],
) -> None:
    """Store a message and its related data. Silently skips if already stored."""
    conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (message_id, guild_id, channel_id, author_id, content, reply_to_id, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, guild_id, channel_id, author_id, content, reply_to_id, ts),
    )
    for url in attachment_urls:
        conn.execute(
            "INSERT OR IGNORE INTO message_attachments (message_id, url) VALUES (?, ?)",
            (message_id, url),
        )
    for user_id in mention_ids:
        conn.execute(
            "INSERT OR IGNORE INTO message_mentions (message_id, user_id) VALUES (?, ?)",
            (message_id, user_id),
        )


def set_reaction_count(
    conn: sqlite3.Connection,
    message_id: int,
    emoji: str,
    count: int,
) -> None:
    """Set an absolute reaction count (used when backfilling from message history)."""
    if count <= 0:
        conn.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND emoji = ?",
            (message_id, emoji),
        )
    else:
        conn.execute(
            """
            INSERT INTO message_reactions (message_id, emoji, count) VALUES (?, ?, ?)
            ON CONFLICT(message_id, emoji) DO UPDATE SET count = excluded.count
            """,
            (message_id, emoji, count),
        )


def adjust_reaction_count(
    conn: sqlite3.Connection,
    message_id: int,
    emoji: str,
    delta: int,
) -> None:
    """Increment or decrement a reaction count for a live reaction event."""
    conn.execute(
        """
        INSERT INTO message_reactions (message_id, emoji, count)
        VALUES (?, ?, MAX(0, ?))
        ON CONFLICT(message_id, emoji) DO UPDATE SET count = MAX(0, count + ?)
        """,
        (message_id, emoji, delta, delta),
    )
    conn.execute(
        "DELETE FROM message_reactions WHERE message_id = ? AND emoji = ? AND count = 0",
        (message_id, emoji),
    )


def delete_message(conn: sqlite3.Connection, message_id: int) -> None:
    """Remove a message and all its associated rows."""
    conn.execute("DELETE FROM message_reactions WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM message_mentions WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM message_attachments WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))


def delete_messages_bulk(conn: sqlite3.Connection, message_ids: set[int]) -> None:
    """Remove multiple messages and their associated rows."""
    if not message_ids:
        return
    ph = ",".join("?" * len(message_ids))
    ids = list(message_ids)
    conn.execute(f"DELETE FROM message_reactions  WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM message_mentions   WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM message_attachments WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM messages            WHERE message_id IN ({ph})", ids)
