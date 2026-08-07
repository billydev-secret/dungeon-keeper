"""Message content archive.

Stores message text, reply references, attachment URLs, @mentions, and
reaction counts so they can be queried by other services (AI review, etc.).

All writes are idempotent — safe to call from both the live event handler
and the /interaction_scan backfill without creating duplicates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence

log = logging.getLogger(__name__)

# ── Per-guild message-content storage levels ──────────────────────────
# Stored in the ``config`` table under STORAGE_LEVEL_KEY, scoped per guild.
# Only NONE (default) and ALL are wired up; AUTOMOD and DELETE_CACHE are
# reserved for a later partial-retention pass and are not yet accepted by
# the config API.
STORAGE_LEVEL_KEY = "message_storage_level"
STORAGE_LEVEL_NONE = "none"
STORAGE_LEVEL_ALL = "all"
STORAGE_LEVEL_AUTOMOD = "automod"  # reserved — not implemented
STORAGE_LEVEL_DELETE_CACHE = "delete_cache"  # reserved — not implemented
# Levels the config API currently accepts.
SUPPORTED_STORAGE_LEVELS = frozenset({STORAGE_LEVEL_NONE, STORAGE_LEVEL_ALL})


# ── Media-kind classification ─────────────────────────────────────────
# A coarse per-message media classification kept as metadata even when raw
# content/attachment URLs are dropped (storage level "none"), so media-based
# metrics (e.g. NSFW posting by gender) keep working without retaining content.
# Derived from attachment file extensions only — no URL or content is stored.
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
# "media" = non-gif image/video (what the NSFW media metric counts).
MEDIA_KIND_MEDIA = "media"
MEDIA_KIND_GIF = "gif"
MEDIA_KIND_OTHER = "other"


def classify_media_kind(filenames: list[str]) -> str | None:
    """Reduce a message's attachment names to a single coarse media_kind.

    Precedence: ``media`` (non-gif image/video) > ``gif`` > ``other``; returns
    ``None`` when there are no attachments. ``filenames`` may be filenames or
    full URLs — only the extension (before any ``?`` query string) is inspected,
    so no URL or content is retained.
    """
    if not filenames:
        return None
    kinds: set[str] = set()
    for name in filenames:
        path = name.split("?", 1)[0].lower()
        dot = path.rfind(".")
        ext = path[dot:] if dot != -1 else ""
        if ext in _IMAGE_EXTENSIONS or ext in _VIDEO_EXTENSIONS:
            kinds.add(MEDIA_KIND_MEDIA)
        elif ext == ".gif":
            kinds.add(MEDIA_KIND_GIF)
        else:
            kinds.add(MEDIA_KIND_OTHER)
    if MEDIA_KIND_MEDIA in kinds:
        return MEDIA_KIND_MEDIA
    if MEDIA_KIND_GIF in kinds:
        return MEDIA_KIND_GIF
    return MEDIA_KIND_OTHER


def guild_retains_content(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    allow_legacy_fallback: bool = True,
) -> bool:
    """True if this guild's storage level keeps raw message content.

    Defaults to False (level ``none``) for any guild that hasn't opted in,
    so derivations (XP, sentiment, interactions) are kept but the message
    text/attachments/embeds are dropped at ingest.
    """
    from bot_modules.core.db_utils import get_config_value

    level = get_config_value(
        conn,
        STORAGE_LEVEL_KEY,
        STORAGE_LEVEL_NONE,
        guild_id,
        allow_legacy_fallback=allow_legacy_fallback,
    )
    return level == STORAGE_LEVEL_ALL


def _flatten_embeds(embeds: list[dict]) -> str | None:
    """Flatten embed dicts into a single searchable text string."""
    parts = []
    for e in embeds:
        for key in ("title", "description", "author", "footer"):
            if e.get(key):
                parts.append(e[key])
        for field in e.get("fields") or []:
            if field.get("name"):
                parts.append(field["name"])
            if field.get("value"):
                parts.append(field["value"])
    return "\n".join(parts) if parts else None


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
            emotion     TEXT,
            media_kind  TEXT,
            deleted_at     INTEGER,
            deleted_source TEXT
        )
        """
    )
    # Migrate existing tables that lack the sentiment columns
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "sentiment" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN sentiment REAL")
    if "emotion" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN emotion TEXT")
    if "deleted_at" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN deleted_at INTEGER")
    if "deleted_source" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN deleted_source TEXT")

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
        "CREATE INDEX IF NOT EXISTS idx_messages_sentiment "
        "ON messages (guild_id, sentiment)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_emotion "
        "ON messages (guild_id, emotion)"
    )
    # Partial: nearly every row is live, and only the deleted minority is ever
    # queried through this column. Keyed on ts rather than deleted_at so the
    # sort reads off the index — see 155_messages_deleted.sql.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_deleted "
        "ON messages (guild_id, ts) WHERE deleted_at IS NOT NULL"
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
        CREATE TABLE IF NOT EXISTS message_embeds (
            message_id  INTEGER NOT NULL,
            embed_index INTEGER NOT NULL,
            title       TEXT,
            description TEXT,
            url         TEXT,
            author_name TEXT,
            footer_text TEXT,
            fields_json TEXT,
            PRIMARY KEY (message_id, embed_index)
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
            is_bot          INTEGER NOT NULL DEFAULT 0,
            current_member  INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # Migrations for existing DBs
    for col, definition in (
        ("is_bot", "INTEGER NOT NULL DEFAULT 0"),
        ("current_member", "INTEGER NOT NULL DEFAULT 1"),
    ):
        try:
            conn.execute(f"ALTER TABLE known_users ADD COLUMN {col} {definition}")
        except Exception:
            log.exception("message_store: add column may already exist")


def upsert_known_user(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    username: str,
    display_name: str,
    ts: float,
    *,
    is_bot: bool = False,
    current_member: bool = True,
) -> None:
    """Insert or update a user's known name. Only updates name fields if ts is newer."""
    conn.execute(
        """
        INSERT INTO known_users (guild_id, user_id, username, display_name, updated_at, is_bot, current_member)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            username = CASE WHEN excluded.updated_at > known_users.updated_at THEN excluded.username ELSE known_users.username END,
            display_name = CASE WHEN excluded.updated_at > known_users.updated_at THEN excluded.display_name ELSE known_users.display_name END,
            updated_at = CASE WHEN excluded.updated_at > known_users.updated_at THEN excluded.updated_at ELSE known_users.updated_at END,
            is_bot = excluded.is_bot,
            current_member = excluded.current_member
        """,
        (guild_id, user_id, username, display_name, ts, int(is_bot), int(current_member)),
    )


def mark_member_left(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "UPDATE known_users SET current_member = 0 WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def init_member_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_events (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            event_type  TEXT NOT NULL,
            ts          REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id, event_type, ts)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_member_events_guild_ts "
        "ON member_events (guild_id, ts)"
    )


def record_member_event(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    event_type: str,
    ts: float,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO member_events (guild_id, user_id, event_type, ts)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, user_id, event_type, ts),
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


def get_known_user_names_bulk(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: list[int],
) -> dict[int, str]:
    """Return {user_id: best_known_name} for a batch, falling back to username.

    Unlike :func:`get_known_users_bulk` this keeps the ``username`` column in
    play, so a row recorded before the user had a guild nickname (blank
    ``display_name``) still yields something readable instead of dropping to a
    raw id. Rows whose name columns are both empty are omitted entirely, so a
    caller can treat "absent from the result" as "no name on file".
    """
    if not user_ids:
        return {}
    ph = ",".join("?" * len(user_ids))
    rows = conn.execute(
        f"SELECT user_id, display_name, username FROM known_users "
        f"WHERE guild_id = ? AND user_id IN ({ph})",
        [guild_id, *user_ids],
    ).fetchall()
    out: dict[int, str] = {}
    for r in rows:
        name = (r["display_name"] or "").strip() or (r["username"] or "").strip()
        if name:
            out[int(r["user_id"])] = name
    return out


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
    embeds: Sequence[dict] = (),
    retain_content: bool = True,
    media_kind: str | None = None,
) -> None:
    """Store a message and its related data. Silently skips if already stored.

    ``embeds`` is a list of embed dicts with keys: title, description, url,
    author, footer, fields (list of {name, value, inline}).  When ``content``
    is None and embeds are present, the flattened embed text is used as content
    so embed-only bot messages remain searchable.

    When ``retain_content`` is False (guild storage level ``none``), the raw
    message text, attachment URLs, and embeds are dropped — only the row
    skeleton (ids/ts), derivations (sentiment/emotion/media_kind), and
    @-mention edges are persisted, leaving a content-less record that still
    reconstructs a Discord deep link. ``media_kind`` is metadata (an attachment
    classification, not a URL), so it is retained regardless of storage level.
    """
    if not retain_content:
        content = None
        attachment_urls = []
        embeds = ()

    # Use flattened embed text as searchable content for embed-only messages.
    if content is None and embeds:
        content = _flatten_embeds(list(embeds))

    conn.execute(
        """
        INSERT INTO messages
            (message_id, guild_id, channel_id, author_id, content, reply_to_id, ts, sentiment, emotion, media_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            content = CASE
                WHEN (messages.content IS NULL OR messages.content = '')
                     AND excluded.content IS NOT NULL
                     AND excluded.content <> ''
                THEN excluded.content
                ELSE messages.content
            END,
            reply_to_id = COALESCE(messages.reply_to_id, excluded.reply_to_id),
            sentiment = COALESCE(excluded.sentiment, messages.sentiment),
            emotion = COALESCE(excluded.emotion, messages.emotion),
            media_kind = COALESCE(excluded.media_kind, messages.media_kind)
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
            media_kind,
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
    for idx, embed in enumerate(embeds):
        conn.execute(
            """
            INSERT OR IGNORE INTO message_embeds
                (message_id, embed_index, title, description, url, author_name, footer_text, fields_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                idx,
                embed.get("title"),
                embed.get("description"),
                embed.get("url"),
                embed.get("author"),
                embed.get("footer"),
                json.dumps(embed.get("fields") or []),
            ),
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


def purge_guild_message_content(conn: sqlite3.Connection, guild_id: int) -> int:
    """Erase stored message *content* for a guild, keeping derivations.

    Nulls ``messages.content`` and removes attachment URLs and embed text for
    every message in the guild. Deliberately leaves the message rows, sentiment
    scores, @-mention edges, and all derived tables (XP, interactions, member
    activity, the processed-message ledger) intact — wiping those would corrupt
    XP back-calculation and analytics.

    Invoked once when a guild is switched to storage level ``none``. Returns the
    number of message rows whose content was cleared.
    """
    id_subq = "SELECT message_id FROM messages WHERE guild_id = ?"
    conn.execute(
        f"DELETE FROM message_attachments WHERE message_id IN ({id_subq})",
        (guild_id,),
    )
    conn.execute(
        f"DELETE FROM message_embeds WHERE message_id IN ({id_subq})",
        (guild_id,),
    )
    cur = conn.execute(
        "UPDATE messages SET content = NULL "
        "WHERE guild_id = ? AND content IS NOT NULL",
        (guild_id,),
    )
    return max(cur.rowcount, 0)


# ── Deletion marking ──────────────────────────────────────────────────
#
# The archive is permanent: a Discord deletion flags the row, it does not
# remove it. The only hard erasure is ``privacy_service.purge_user_data`` — a
# subject's Art 17 request must actually erase, and a flag would not.

# A deletion we observed but cannot attribute — Discord's raw delete payload
# names no actor. This covers a member deleting their own message, a mod
# removing one, and a member's privacy-panel purge alike.
DELETE_SOURCE_DISCORD = "discord"
# Our own auto-delete sweep expiring a channel's messages on a timer. High
# volume and routine, which is exactly why it is worth telling apart.
DELETE_SOURCE_AUTO_DELETE = "auto_delete"

# Deliberately only two. A member's privacy-panel purge is *not* distinguished:
# ``privacy_cog._run_deletion`` is guaranteed — in a comment and in
# test_privacy_modes.test_no_mode_touches_the_database — never to open the DB,
# and stamping a source would break that guard. It would also mean the archive
# recorded that a member had exercised a privacy control, which is a new kind
# of data about them for a mod-visible surface. Their purge lands as
# ``discord`` like any other deletion.
DELETE_SOURCES = frozenset({DELETE_SOURCE_DISCORD, DELETE_SOURCE_AUTO_DELETE})


def mark_messages_deleted(
    conn: sqlite3.Connection,
    guild_id: int,
    message_ids: set[int] | list[int],
    source: str,
    ts: int,
) -> int:
    """Flag messages as deleted. Returns how many rows this call newly flagged.

    First writer wins: the ``deleted_at IS NULL`` guard makes the write both
    idempotent (a redelivered gateway event is a no-op) and non-clobbering.
    That guard is what makes attribution work at all — ``auto_delete_service``
    and the privacy purge stamp their ids *before* calling the Discord API, so
    the generic ``on_raw_message_delete`` that follows finds the row already
    flagged and leaves the specific source in place. Without it, every
    attributed deletion would be overwritten with ``discord`` moments later.

    Messages we have no row for are silently skipped — the guild may have been
    at storage level ``none``, or the message may predate the bot.
    """
    if not message_ids:
        return 0
    if source not in DELETE_SOURCES:
        raise ValueError(f"unknown delete source: {source!r}")
    ids = list(message_ids)
    ph = ",".join("?" * len(ids))
    cur = conn.execute(
        f"""
        UPDATE messages
           SET deleted_at = ?, deleted_source = ?
         WHERE message_id IN ({ph})
           AND guild_id = ?
           AND deleted_at IS NULL
        """,
        (ts, source, *ids, guild_id),
    )
    return max(cur.rowcount, 0)


def clear_deleted_flag(
    conn: sqlite3.Connection,
    guild_id: int,
    message_ids: set[int] | list[int],
    source: str,
) -> int:
    """Undo a claim for messages that were never actually deleted.

    The claim-then-delete order guarantees attribution but is optimistic: if
    Discord refuses the delete (lost permissions, a transient 5xx), the message
    is still there and a "deleted" badge would be a lie — one that also
    suppresses the deep link to a message a moderator could otherwise open.

    Scoped to ``source`` so a rollback can only ever clear our own optimistic
    claim. A row already flagged ``discord`` was deleted by someone else while
    we were working, and must survive this.
    """
    if not message_ids:
        return 0
    ids = list(message_ids)
    ph = ",".join("?" * len(ids))
    cur = conn.execute(
        f"""
        UPDATE messages
           SET deleted_at = NULL, deleted_source = NULL
         WHERE message_id IN ({ph})
           AND guild_id = ?
           AND deleted_source = ?
        """,
        (*ids, guild_id, source),
    )
    return max(cur.rowcount, 0)


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
    from bot_modules.core.xp_system import MemberActivity

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
