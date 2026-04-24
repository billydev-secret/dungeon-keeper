"""Confessions service — DB layer and pure helpers ported from openConfess."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord

from db_utils import open_db

DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_MAX_CHARS = 2000
DEFAULT_MAX_ATTACHMENTS = 4
THREAD_METADATA_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_REPLY_COOLDOWN_SECONDS = 30
CONFESSION_HEADER_LENGTH = 2
MAX_DISCORD_MESSAGE_LENGTH = 2000

ERROR_NOT_CONFIGURED = "Bot is not configured. Ask an admin to set destination/log channels."
ERROR_CONFIG_INVALID = "Bot configuration is invalid. Contact an administrator."
ERROR_PANIC_MODE = "Confessions are temporarily disabled."
ERROR_USER_BLOCKED = "You can't submit confessions on this server."
ERROR_REPLIES_DISABLED = "Anonymous replies are disabled on this server."
ERROR_NOT_SETUP = "Guild not configured: run /confession set-dest and /confession set-log first."


def now_ts() -> int:
    return int(time.time())


def defang_everyone_here(text: str) -> str:
    return (
        text.replace("@everyone", "@​everyone")
            .replace("@here", "@​here")
    )


def thread_name_from_content(content: str, max_len: int = 100) -> str:
    name = " ".join(content.split())
    if len(name) > max_len:
        name = name[:max_len - 1].rstrip() + "…"
    return name or "Anonymous Confession"


def jump_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


_ANON_ADJECTIVES = [
    "Anonymous", "Secret", "Mysterious", "Hidden", "Silent", "Sneaky", "Curious",
    "Wandering", "Sleepy", "Bouncy", "Grumpy", "Jolly", "Fluffy", "Spooky",
    "Zesty", "Cosmic", "Fuzzy", "Mighty", "Tiny", "Brave",
]
_ANON_ANIMALS = [
    "Aardvark", "Albatross", "Axolotl", "Badger", "Capybara", "Chameleon",
    "Dingo", "Echidna", "Flamingo", "Gecko", "Hedgehog", "Iguana", "Jaguar",
    "Kinkajou", "Lemur", "Manatee", "Narwhal", "Ocelot", "Pangolin", "Quokka",
    "Raccoon", "Salamander", "Tapir", "Uakari", "Vicuna", "Wombat", "Xerus",
    "Yak", "Zorilla", "Platypus", "Capuchin", "Dugong", "Fennec", "Gibbon",
]
_ANON_CIRCLES = [
    "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪",
    "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬛", "⬜",
    "🔶", "🔷", "🔸", "🔹",
]
_OP_CIRCLE = "⭐"


def anon_id(user_id: int, root_message_id: int) -> str:
    digest = hashlib.sha256(f"{user_id}:{root_message_id}".encode()).digest()
    adj = _ANON_ADJECTIVES[int.from_bytes(digest[0:2], "big") % len(_ANON_ADJECTIVES)]
    animal = _ANON_ANIMALS[int.from_bytes(digest[2:4], "big") % len(_ANON_ANIMALS)]
    return f"{adj} {animal}"


def anon_circle(user_id: int, root_message_id: int) -> str:
    digest = hashlib.sha256(f"c:{user_id}:{root_message_id}".encode()).digest()
    return _ANON_CIRCLES[int.from_bytes(digest[:2], "big") % len(_ANON_CIRCLES)]


def build_anon_reply(
    content: str,
    user_id: int,
    root_message_id: int,
    *,
    is_op: bool,
    circle: Optional[str] = None,
) -> str:
    safe = defang_everyone_here(content)
    if is_op:
        prefix = f"{_OP_CIRCLE} [OP]"
    else:
        resolved_circle = circle if circle is not None else anon_circle(user_id, root_message_id)
        tag = anon_id(user_id, root_message_id)
        prefix = f"{resolved_circle} {tag}"
    msg = f"{prefix}\n{safe}"
    if len(msg) > MAX_DISCORD_MESSAGE_LENGTH:
        msg = f"{prefix}\n{safe[:MAX_DISCORD_MESSAGE_LENGTH - len(prefix) - 1]}"
    return msg


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

@dataclass
class GuildConfig:
    guild_id: int
    dest_channel_id: int
    log_channel_id: int
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    max_chars: int = DEFAULT_MAX_CHARS
    max_attachments: int = DEFAULT_MAX_ATTACHMENTS
    panic: bool = False
    replies_enabled: bool = True
    notify_op_on_reply: bool = False
    per_day_limit: int = 0
    launcher_channel_id: int = 0
    launcher_message_id: int = 0
    blocked_user_ids: Optional[list[int]] = None

    def blocked_set(self) -> set[int]:
        return set(self.blocked_user_ids or [])


def init_db(db_path: Path) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_config (
            guild_id INTEGER PRIMARY KEY,
            dest_channel_id INTEGER NOT NULL DEFAULT 0,
            log_channel_id INTEGER NOT NULL DEFAULT 0,
            cooldown_seconds INTEGER NOT NULL DEFAULT 120,
            max_chars INTEGER NOT NULL DEFAULT 2000,
            max_attachments INTEGER NOT NULL DEFAULT 4,
            panic INTEGER NOT NULL DEFAULT 0,
            replies_enabled INTEGER NOT NULL DEFAULT 1,
            notify_op_on_reply INTEGER NOT NULL DEFAULT 0,
            per_day_limit INTEGER NOT NULL DEFAULT 0,
            launcher_channel_id INTEGER NOT NULL DEFAULT 0,
            launcher_message_id INTEGER NOT NULL DEFAULT 0,
            blocked_user_ids TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_rate_limits (
            guild_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            last_confess_at INTEGER NOT NULL DEFAULT 0,
            last_reply_at INTEGER NOT NULL DEFAULT 0,
            day_key TEXT NOT NULL DEFAULT '',
            day_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, author_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_threads (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            root_message_id INTEGER NOT NULL,
            original_author_id INTEGER NOT NULL,
            notify_original_author INTEGER NOT NULL DEFAULT -1,
            discord_thread_id INTEGER NOT NULL DEFAULT 0,
            reply_button_message_id INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, message_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_confession_threads_created_at ON confession_threads(created_at)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_emoji_assignments (
            guild_id INTEGER NOT NULL,
            root_message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            emoji_index INTEGER NOT NULL,
            PRIMARY KEY (guild_id, root_message_id, user_id)
        )
    """)


def _row_to_guild_config(row) -> GuildConfig:
    return GuildConfig(
        guild_id=row["guild_id"],
        dest_channel_id=row["dest_channel_id"],
        log_channel_id=row["log_channel_id"],
        cooldown_seconds=row["cooldown_seconds"],
        max_chars=row["max_chars"],
        max_attachments=row["max_attachments"],
        panic=bool(row["panic"]),
        replies_enabled=bool(row["replies_enabled"]),
        notify_op_on_reply=bool(row["notify_op_on_reply"]),
        per_day_limit=row["per_day_limit"],
        launcher_channel_id=row["launcher_channel_id"],
        launcher_message_id=row["launcher_message_id"],
        blocked_user_ids=json.loads(row["blocked_user_ids"] or "[]"),
    )


def get_config(db_path: Path, guild_id: int) -> Optional[GuildConfig]:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM confession_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    return _row_to_guild_config(row) if row else None


def get_config_conn(conn, guild_id: int) -> Optional[GuildConfig]:
    """Read confession config using an already-open connection."""
    row = conn.execute(
        "SELECT * FROM confession_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    return _row_to_guild_config(row) if row else None


def upsert_config(db_path: Path, cfg: GuildConfig) -> None:
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO confession_config (
                guild_id, dest_channel_id, log_channel_id, cooldown_seconds,
                max_chars, max_attachments, panic, replies_enabled, notify_op_on_reply,
                per_day_limit, launcher_channel_id, launcher_message_id, blocked_user_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                dest_channel_id=excluded.dest_channel_id,
                log_channel_id=excluded.log_channel_id,
                cooldown_seconds=excluded.cooldown_seconds,
                max_chars=excluded.max_chars,
                max_attachments=excluded.max_attachments,
                panic=excluded.panic,
                replies_enabled=excluded.replies_enabled,
                notify_op_on_reply=excluded.notify_op_on_reply,
                per_day_limit=excluded.per_day_limit,
                launcher_channel_id=excluded.launcher_channel_id,
                launcher_message_id=excluded.launcher_message_id,
                blocked_user_ids=excluded.blocked_user_ids
        """, (
            cfg.guild_id, cfg.dest_channel_id, cfg.log_channel_id,
            cfg.cooldown_seconds, cfg.max_chars, cfg.max_attachments,
            int(cfg.panic), int(cfg.replies_enabled), int(cfg.notify_op_on_reply),
            cfg.per_day_limit, cfg.launcher_channel_id, cfg.launcher_message_id,
            json.dumps(cfg.blocked_user_ids or []),
        ))


def upsert_thread_post(
    db_path: Path,
    guild_id: int,
    message_id: int,
    channel_id: int,
    root_message_id: int,
    original_author_id: int,
    notify_original_author: int = -1,
) -> None:
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO confession_threads (
                guild_id, message_id, channel_id, root_message_id,
                original_author_id, notify_original_author, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, message_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                root_message_id=excluded.root_message_id,
                original_author_id=excluded.original_author_id,
                notify_original_author=excluded.notify_original_author,
                created_at=excluded.created_at
        """, (
            guild_id, message_id, channel_id, root_message_id,
            original_author_id, notify_original_author, now_ts(),
        ))


def get_thread_info(
    db_path: Path, guild_id: int, message_id: int
) -> Optional[tuple[int, int, int]]:
    with open_db(db_path) as conn:
        row = conn.execute("""
            SELECT root_message_id, original_author_id, notify_original_author
            FROM confession_threads WHERE guild_id = ? AND message_id = ?
        """, (guild_id, message_id)).fetchone()
    if not row:
        return None
    return (int(row["root_message_id"]), int(row["original_author_id"]), int(row["notify_original_author"]))


def get_discord_thread_id(db_path: Path, guild_id: int, root_message_id: int) -> int:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT discord_thread_id FROM confession_threads WHERE guild_id = ? AND message_id = ?",
            (guild_id, root_message_id),
        ).fetchone()
    return int(row["discord_thread_id"]) if row else 0


def update_discord_thread_id(db_path: Path, guild_id: int, root_message_id: int, thread_id: int) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE confession_threads SET discord_thread_id = ? WHERE guild_id = ? AND message_id = ?",
            (thread_id, guild_id, root_message_id),
        )


def get_reply_button_message_id(db_path: Path, guild_id: int, root_message_id: int) -> int:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT reply_button_message_id FROM confession_threads WHERE guild_id = ? AND message_id = ?",
            (guild_id, root_message_id),
        ).fetchone()
    return int(row["reply_button_message_id"]) if row else 0


def update_reply_button_message_id(
    db_path: Path, guild_id: int, root_message_id: int, button_message_id: int
) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE confession_threads SET reply_button_message_id = ? WHERE guild_id = ? AND message_id = ?",
            (button_message_id, guild_id, root_message_id),
        )


def get_or_assign_emoji_index(db_path: Path, guild_id: int, root_message_id: int, user_id: int) -> int:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT emoji_index FROM confession_emoji_assignments WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
            (guild_id, root_message_id, user_id),
        ).fetchone()
        if row:
            return int(row["emoji_index"])
        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM confession_emoji_assignments WHERE guild_id = ? AND root_message_id = ?",
            (guild_id, root_message_id),
        ).fetchone()
        idx = int(count_row["cnt"]) % len(_ANON_CIRCLES)
        conn.execute(
            "INSERT OR IGNORE INTO confession_emoji_assignments (guild_id, root_message_id, user_id, emoji_index) VALUES (?, ?, ?, ?)",
            (guild_id, root_message_id, user_id, idx),
        )
        row = conn.execute(
            "SELECT emoji_index FROM confession_emoji_assignments WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
            (guild_id, root_message_id, user_id),
        ).fetchone()
        return int(row["emoji_index"])


def check_and_bump_limits(
    db_path: Path,
    guild_id: int,
    author_id: int,
    *,
    is_reply: bool,
    cooldown_seconds: int,
    per_day_limit: int,
) -> tuple[bool, str]:
    now = now_ts()
    day_key = time.strftime("%Y-%m-%d", time.gmtime())
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM confession_rate_limits WHERE guild_id = ? AND author_id = ?",
            (guild_id, author_id),
        ).fetchone()
        last_confess_at, last_reply_at, stored_day_key, day_count = 0, 0, day_key, 0
        if row:
            last_confess_at = row["last_confess_at"]
            last_reply_at = row["last_reply_at"]
            stored_day_key = row["day_key"]
            day_count = row["day_count"]
        if stored_day_key != day_key:
            day_count = 0
            stored_day_key = day_key
        last_at = last_reply_at if is_reply else last_confess_at
        if cooldown_seconds > 0 and (now - last_at) < cooldown_seconds:
            remaining = cooldown_seconds - (now - last_at)
            verb = "reply" if is_reply else "post"
            return False, f"Slow down — you can {verb} again in **{remaining}s**."
        if per_day_limit > 0 and day_count >= per_day_limit:
            return False, f"You've hit today's limit (**{per_day_limit}**). Try again tomorrow."
        if is_reply:
            last_reply_at = now
        else:
            last_confess_at = now
            day_count += 1
        conn.execute("""
            INSERT INTO confession_rate_limits (guild_id, author_id, last_confess_at, last_reply_at, day_key, day_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, author_id) DO UPDATE SET
                last_confess_at=excluded.last_confess_at,
                last_reply_at=excluded.last_reply_at,
                day_key=excluded.day_key,
                day_count=excluded.day_count
        """, (guild_id, author_id, last_confess_at, last_reply_at, stored_day_key, day_count))
    return True, "ok"


def purge_old_thread_posts(db_path: Path, max_age_seconds: int = THREAD_METADATA_TTL_SECONDS) -> int:
    cutoff = now_ts() - max_age_seconds
    with open_db(db_path) as conn:
        cur = conn.execute("DELETE FROM confession_threads WHERE created_at < ?", (cutoff,))
        return max(cur.rowcount, 0)


# ---------------------------------------------------------------------------
# Embed helpers
# ---------------------------------------------------------------------------

async def log_confession(
    *,
    log_channel: discord.TextChannel,
    author: discord.Member | discord.User,
    guild_id: int,
    dest_channel_id: int,
    dest_message_id: int,
    content: str,
) -> Optional[discord.Message]:
    emb = discord.Embed(
        title="Logged Confession",
        description="(Private log entry)",
        timestamp=discord.utils.utcnow(),
    )
    emb.add_field(name="Author", value=f"{author.mention} (`{author.id}`)", inline=False)
    emb.add_field(
        name="Posted",
        value=f"<#{dest_channel_id}>\n{jump_link(guild_id, dest_channel_id, dest_message_id)}",
        inline=False,
    )
    emb.add_field(name="Content", value=content[:1024], inline=False)
    emb.add_field(
        name="Meta",
        value=f"guild_id={guild_id}\nchannel_id={dest_channel_id}\nmessage_id={dest_message_id}",
        inline=False,
    )
    try:
        return await log_channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        return None


async def log_reply(
    *,
    log_channel: discord.TextChannel,
    author: discord.Member | discord.User,
    guild_id: int,
    parent_channel_id: int,
    parent_message_id: int,
    reply_channel_id: int,
    reply_message_id: int,
    content: str,
) -> Optional[discord.Message]:
    emb = discord.Embed(
        title="Logged Reply",
        description="(Private log entry)",
        timestamp=discord.utils.utcnow(),
    )
    emb.add_field(name="Author", value=f"{author.mention} (`{author.id}`)", inline=False)
    emb.add_field(name="Parent", value=jump_link(guild_id, parent_channel_id, parent_message_id), inline=False)
    emb.add_field(name="Reply", value=jump_link(guild_id, reply_channel_id, reply_message_id), inline=False)
    emb.add_field(name="Content", value=content[:1024], inline=False)
    emb.add_field(name="Meta", value=f"guild_id={guild_id}", inline=False)
    try:
        return await log_channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        return None
