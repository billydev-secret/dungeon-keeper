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
            ts          INTEGER NOT NULL,
            sentiment   REAL,
            emotion     TEXT
        )
        """
    )
    # Migrate existing tables that lack the sentiment columns
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "sentiment" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN sentiment REAL")
    if "emotion" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN emotion TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_guild_ts ON messages (guild_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_author "
        "ON messages (guild_id, author_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_channel_ts "
        "ON messages (guild_id, channel_id, ts)"
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
        "CREATE INDEX IF NOT EXISTS idx_mentions_user ON message_mentions (user_id)"
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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reaction_log (
            guild_id    INTEGER NOT NULL,
            reactor_id  INTEGER NOT NULL,
            author_id   INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            ts          INTEGER NOT NULL,
            PRIMARY KEY (guild_id, message_id, reactor_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reaction_log_guild_ts "
        "ON reaction_log (guild_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reaction_log_reactor "
        "ON reaction_log (guild_id, reactor_id)"
    )


def init_known_users_table(conn: sqlite3.Connection) -> None:
    """Create the known_users lookup table for offline username resolution."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS known_users (
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            username        TEXT NOT NULL DEFAULT '',
            display_name    TEXT NOT NULL DEFAULT '',
            updated_at      REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


def upsert_known_user(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    username: str,
    display_name: str,
    ts: float,
) -> None:
    """Insert or update a user's known name. Only updates if ts is newer."""
    conn.execute(
        """
        INSERT INTO known_users (guild_id, user_id, username, display_name, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            username = excluded.username,
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        WHERE excluded.updated_at > known_users.updated_at
        """,
        (guild_id, user_id, username, display_name, ts),
    )


def get_known_user(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> str | None:
    """Return display_name for a user, or None if unknown."""
    row = conn.execute(
        "SELECT display_name FROM known_users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return row[0] if row else None


def get_known_users_bulk(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: list[int],
) -> dict[int, str]:
    """Return {user_id: display_name} for a batch of users."""
    if not user_ids:
        return {}
    ph = ",".join("?" * len(user_ids))
    rows = conn.execute(
        f"SELECT user_id, display_name FROM known_users WHERE guild_id = ? AND user_id IN ({ph})",
        [guild_id, *user_ids],
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def init_known_channels_table(conn: sqlite3.Connection) -> None:
    """Create the known_channels lookup table for offline channel name resolution."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS known_channels (
            guild_id        INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL,
            channel_name    TEXT NOT NULL DEFAULT '',
            updated_at      REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )


def upsert_known_channel(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    ts: float,
) -> None:
    """Insert or update a channel's known name. Only updates if ts is newer."""
    conn.execute(
        """
        INSERT INTO known_channels (guild_id, channel_id, channel_name, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET
            channel_name = excluded.channel_name,
            updated_at = excluded.updated_at
        WHERE excluded.updated_at > known_channels.updated_at
        """,
        (guild_id, channel_id, channel_name, ts),
    )


def get_known_channels_bulk(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_ids: list[int],
) -> dict[int, str]:
    """Return {channel_id: channel_name} for a batch of channels."""
    if not channel_ids:
        return {}
    ph = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"SELECT channel_id, channel_name FROM known_channels WHERE guild_id = ? AND channel_id IN ({ph})",
        [guild_id, *channel_ids],
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


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
    sentiment: float | None = None,
    emotion: str | None = None,
) -> None:
    """Store a message and its related data. Silently skips if already stored."""
    conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (message_id, guild_id, channel_id, author_id, content, reply_to_id, ts, sentiment, emotion)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            guild_id,
            channel_id,
            author_id,
            content,
            reply_to_id,
            ts,
            sentiment,
            emotion,
        ),
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


def record_reaction(
    conn: sqlite3.Connection,
    guild_id: int,
    reactor_id: int,
    author_id: int,
    channel_id: int,
    message_id: int,
    ts: int,
) -> None:
    """Record an individual reaction event for quality scoring."""
    if reactor_id == author_id:
        return  # Self-reactions excluded
    conn.execute(
        """
        INSERT OR IGNORE INTO reaction_log
            (guild_id, reactor_id, author_id, channel_id, message_id, ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (guild_id, reactor_id, author_id, channel_id, message_id, ts),
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
    conn.execute("DELETE FROM message_sentiment WHERE message_id = ?", (message_id,))
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
    conn.execute(f"DELETE FROM message_sentiment  WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM messages            WHERE message_id IN ({ph})", ids)


# GIF / image-link patterns: Tenor, Giphy, Imgur GIFs, Discord CDN GIFs, bare .gif URLs
_GIF_PATTERNS = (
    "://tenor.com/",
    "://giphy.com/",
    "://media.giphy.com/",
    "://i.imgur.com/",
    ".gif",
)


def _is_gif_only(content: str | None, has_attachment: bool) -> bool:
    """Return True if a message contains nothing but a GIF/image link."""
    if not content:
        return has_attachment  # attachment-only with no text = media-only
    text = content.strip()
    if not text:
        return has_attachment
    # Single URL that looks like a GIF service
    if " " not in text and text.startswith("http"):
        lower = text.lower()
        return any(p in lower for p in _GIF_PATTERNS)
    return False


def query_last_substantive_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: list[int],
    *,
    channel_id: int | None = None,
    exclude_gif_only: bool = False,
) -> dict:
    """Like get_member_last_activity_map but with channel and GIF-only filters.

    Returns dict[int, MemberActivity].
    """
    from xp_system import MemberActivity

    if not user_ids:
        return {}

    activity_map: dict[int, MemberActivity] = {}
    batch_size = 800

    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i : i + batch_size]
        ph = ",".join("?" for _ in batch)

        channel_clause = ""
        params: list[object] = [guild_id, *batch]
        if channel_id is not None:
            channel_clause = " AND m.channel_id = ?"
            params.append(channel_id)

        rows = conn.execute(
            f"""
            SELECT m.author_id, m.channel_id, m.message_id, m.ts, m.content,
                   EXISTS(SELECT 1 FROM message_attachments a WHERE a.message_id = m.message_id) AS has_attach
            FROM messages m
            WHERE m.guild_id = ? AND m.author_id IN ({ph}){channel_clause}
            ORDER BY m.ts DESC
            """,
            params,
        ).fetchall()

        # Walk rows (newest first) and pick the first qualifying message per user
        for row in rows:
            uid = int(row["author_id"])
            if uid in activity_map:
                continue
            if exclude_gif_only and _is_gif_only(
                row["content"], bool(row["has_attach"])
            ):
                continue
            activity_map[uid] = MemberActivity(
                user_id=uid,
                channel_id=int(row["channel_id"]),
                message_id=int(row["message_id"]),
                created_at=float(row["ts"]),
            )

    return activity_map
