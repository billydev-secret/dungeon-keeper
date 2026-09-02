"""Confessions service — DB layer and pure helpers ported from openConfess."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord

from bot_modules.core.db_utils import open_db, open_db_immediate
# Re-exported under the local name this module has always used; the canonical
# definition moved to core.utils when Event Echo became the fourth copy.
from bot_modules.core.utils import jump_url as jump_link

DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_MAX_CHARS = 2000
THREAD_METADATA_TTL_SECONDS = 7 * 24 * 60 * 60
#: How long a confession may sit unreviewed in the approval queue before it
#: is swept. The same seven days, and not by coincidence: manual.html promises
#: members that the link between a confession and its author self-destructs
#: after a week, and a pending row *is* that link — text and author id in one
#: place. A queue nobody works must not quietly become the exception.
PENDING_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_REPLY_COOLDOWN_SECONDS = 30
CONFESSION_HEADER_LENGTH = 2
MAX_DISCORD_MESSAGE_LENGTH = 2000
MAX_EMBED_DESCRIPTION_LENGTH = 4096

ERROR_NOT_CONFIGURED = "❌ Bot is not configured. Ask an admin to set destination/log channels."
ERROR_CONFIG_INVALID = "❌ Bot configuration is invalid. Contact an administrator."
ERROR_PANIC_MODE = "❌ Confessions are temporarily disabled."
ERROR_USER_BLOCKED = "❌ You can't submit confessions on this server."
ERROR_REPLIES_DISABLED = "❌ Anonymous replies are disabled on this server."
ERROR_NOT_SETUP = "❌ Not configured yet: run /confession set-dest and /confession set-log first."


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

_NAME_POOL_SIZE = len(_ANON_ADJECTIVES) * len(_ANON_ANIMALS)
_COLOR_POOL_SIZE = len(_ANON_CIRCLES)



def anon_name_from_index(name_index: int) -> str:
    return f"{_ANON_ADJECTIVES[name_index // len(_ANON_ANIMALS)]} {_ANON_ANIMALS[name_index % len(_ANON_ANIMALS)]}"


def anon_circle_from_index(emoji_index: int) -> str:
    return _ANON_CIRCLES[emoji_index]


def pop_pool_index(
    conn: sqlite3.Connection,
    guild_id: int,
    root_message_id: int,
    pool_type: str,
    pool_size: int,
) -> int:
    """Pop the next index from a per-thread shuffled pool, reshuffling when exhausted."""
    row = conn.execute(
        "SELECT remaining_json, cycle FROM confession_pools "
        "WHERE guild_id = ? AND root_message_id = ? AND pool_type = ?",
        (guild_id, root_message_id, pool_type),
    ).fetchone()
    cycle = row["cycle"] if row else 0
    remaining: list[int] = json.loads(row["remaining_json"]) if row else []
    if not remaining:
        remaining = list(range(pool_size))
        random.shuffle(remaining)
        if row:
            cycle += 1
    idx = remaining.pop()
    conn.execute("""
        INSERT INTO confession_pools (guild_id, root_message_id, pool_type, remaining_json, cycle)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, root_message_id, pool_type) DO UPDATE SET
            remaining_json = excluded.remaining_json,
            cycle = excluded.cycle
    """, (guild_id, root_message_id, pool_type, json.dumps(remaining), cycle))
    return idx


def get_or_assign_anon_identity(
    db_path: Path, guild_id: int, root_message_id: int, user_id: int
) -> tuple[int, int]:
    """Return (name_index, emoji_index) for a persistent anonymous identity in a thread.

    Legacy rows with name_index=-1 are backfilled using the original hash algorithm.
    """
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
            (guild_id, root_message_id, user_id),
        ).fetchone()
        if row:
            name_idx = int(row["name_index"])
            emoji_idx = int(row["emoji_index"])
            if name_idx == -1:
                # Legacy row: derive name_index from original hash, preserve emoji_index
                digest = hashlib.sha256(f"{user_id}:{root_message_id}".encode()).digest()
                adj_idx = int.from_bytes(digest[0:2], "big") % len(_ANON_ADJECTIVES)
                animal_idx = int.from_bytes(digest[2:4], "big") % len(_ANON_ANIMALS)
                name_idx = adj_idx * len(_ANON_ANIMALS) + animal_idx
                conn.execute(
                    "UPDATE confession_emoji_assignments SET name_index = ? "
                    "WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
                    (name_idx, guild_id, root_message_id, user_id),
                )
            return name_idx, emoji_idx
        name_idx = pop_pool_index(conn, guild_id, root_message_id, "name", _NAME_POOL_SIZE)
        emoji_idx = pop_pool_index(conn, guild_id, root_message_id, "color", _COLOR_POOL_SIZE)
        conn.execute(
            "INSERT OR IGNORE INTO confession_emoji_assignments "
            "(guild_id, root_message_id, user_id, emoji_index, name_index) VALUES (?, ?, ?, ?, ?)",
            (guild_id, root_message_id, user_id, emoji_idx, name_idx),
        )
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
            (guild_id, root_message_id, user_id),
        ).fetchone()
        return int(row["name_index"]), int(row["emoji_index"])


def get_ephemeral_anon_identity(db_path: Path, guild_id: int, root_message_id: int) -> tuple[int, int]:
    """Return (name_index, emoji_index) for a one-shot ephemeral identity; not stored."""
    with open_db(db_path) as conn:
        name_idx = pop_pool_index(conn, guild_id, root_message_id, "name", _NAME_POOL_SIZE)
        emoji_idx = pop_pool_index(conn, guild_id, root_message_id, "color", _COLOR_POOL_SIZE)
    return name_idx, emoji_idx


def build_confession_embed(content: str, *, color: "discord.Color") -> "discord.Embed":
    """Build the public embed for a posted confession.

    Titled a plain ``Anonymous Confession`` (no sequential number). The
    body is rendered as-is (no blockquote) so the user's own formatting
    is preserved. ``@everyone``/``@here`` are defanged for safety even
    though embed descriptions can't ping.
    """
    safe = defang_everyone_here(content)
    if len(safe) > MAX_EMBED_DESCRIPTION_LENGTH:
        safe = safe[:MAX_EMBED_DESCRIPTION_LENGTH - 1].rstrip() + "…"
    return discord.Embed(title="🤫 Anonymous Confession", description=safe, color=color)


def build_anon_reply(
    content: str,
    *,
    is_op: bool,
    circle: Optional[str] = None,
    anon_name: Optional[str] = None,
) -> str:
    safe = defang_everyone_here(content)
    if is_op:
        prefix = f"{_OP_CIRCLE} [OP]"
    else:
        prefix = f"{circle} {anon_name}"
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
    panic: bool = False
    replies_enabled: bool = True
    notify_op_on_reply: bool = False
    per_day_limit: int = 0
    launcher_channel_id: int = 0
    launcher_message_id: int = 0
    blocked_user_ids: Optional[list[int]] = None
    #: Hold every new confession for a moderator instead of posting it.
    #: All-or-nothing by design: no age, role or word-list exemption, so
    #: there is no rule that can silently fail open on the one submission
    #: that needed catching.
    require_approval: bool = False

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
            -- NOTE: no max_attachments here. Confessions are submitted through
            -- a Discord modal, which has no attachment path at all; the column
            -- was dropped in migration 194.
            panic INTEGER NOT NULL DEFAULT 0,
            replies_enabled INTEGER NOT NULL DEFAULT 1,
            notify_op_on_reply INTEGER NOT NULL DEFAULT 0,
            per_day_limit INTEGER NOT NULL DEFAULT 0,
            launcher_channel_id INTEGER NOT NULL DEFAULT 0,
            launcher_message_id INTEGER NOT NULL DEFAULT 0,
            blocked_user_ids TEXT NOT NULL DEFAULT '[]',
            require_approval INTEGER NOT NULL DEFAULT 0
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
            guild_id        INTEGER NOT NULL,
            root_message_id INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            emoji_index     INTEGER NOT NULL,
            name_index      INTEGER NOT NULL DEFAULT -1,
            PRIMARY KEY (guild_id, root_message_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_pools (
            guild_id        INTEGER NOT NULL,
            root_message_id INTEGER NOT NULL,
            pool_type       TEXT NOT NULL,
            remaining_json  TEXT NOT NULL DEFAULT '[]',
            cycle           INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, root_message_id, pool_type)
        )
    """)
    # The approval queue. Unlike every other table here this one holds the
    # confession *body* next to the real author id, so it is written only while
    # a submission is waiting and deleted the instant it is resolved — see
    # ``resolve_pending_confession`` and ``purge_expired_pending``.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            notify_original_author INTEGER NOT NULL DEFAULT -1,
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_confession_pending_queue "
        "ON confession_pending(guild_id, created_at)"
    )


def _row_to_guild_config(row) -> GuildConfig:
    return GuildConfig(
        guild_id=row["guild_id"],
        dest_channel_id=row["dest_channel_id"],
        log_channel_id=row["log_channel_id"],
        cooldown_seconds=row["cooldown_seconds"],
        max_chars=row["max_chars"],
        panic=bool(row["panic"]),
        replies_enabled=bool(row["replies_enabled"]),
        notify_op_on_reply=bool(row["notify_op_on_reply"]),
        per_day_limit=row["per_day_limit"],
        launcher_channel_id=row["launcher_channel_id"],
        launcher_message_id=row["launcher_message_id"],
        blocked_user_ids=json.loads(row["blocked_user_ids"] or "[]"),
        # Tolerated by key rather than assumed: a config row read from a DB
        # that predates migration 200 simply has approval off.
        require_approval=bool(row["require_approval"])
        if "require_approval" in row.keys()
        else False,
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
                max_chars, panic, replies_enabled, notify_op_on_reply,
                per_day_limit, launcher_channel_id, launcher_message_id, blocked_user_ids,
                require_approval
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                dest_channel_id=excluded.dest_channel_id,
                log_channel_id=excluded.log_channel_id,
                cooldown_seconds=excluded.cooldown_seconds,
                max_chars=excluded.max_chars,
                panic=excluded.panic,
                replies_enabled=excluded.replies_enabled,
                notify_op_on_reply=excluded.notify_op_on_reply,
                per_day_limit=excluded.per_day_limit,
                launcher_channel_id=excluded.launcher_channel_id,
                launcher_message_id=excluded.launcher_message_id,
                blocked_user_ids=excluded.blocked_user_ids,
                require_approval=excluded.require_approval
        """, (
            cfg.guild_id, cfg.dest_channel_id, cfg.log_channel_id,
            cfg.cooldown_seconds, cfg.max_chars,
            int(cfg.panic), int(cfg.replies_enabled), int(cfg.notify_op_on_reply),
            cfg.per_day_limit, cfg.launcher_channel_id, cfg.launcher_message_id,
            json.dumps(cfg.blocked_user_ids or []), int(cfg.require_approval),
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
# The approval queue
# ---------------------------------------------------------------------------
#
# With ``require_approval`` on, a submission lands here instead of in the
# destination channel and waits for a moderator to approve it from the sticky
# todo board. Two rules shape everything below.
#
# **The row is transient.** It is the only place the bot holds confession text
# next to the real author id — ``anon_audit_log`` stores no content at all and
# ``confession_threads`` keeps routing metadata — so it is deleted on approve,
# deleted on reject, and swept at ``PENDING_TTL_SECONDS`` if nobody ever gets
# to it. Nothing accumulates and there is no rejected-confession archive.
#
# **The row is claimed, not read-then-acted-on.** ``resolve_pending_confession``
# deletes and returns in one immediate transaction, so two moderators pressing
# Approve at the same moment cannot post the same confession twice; the second
# gets ``None`` and is told it has already been handled.
#
# Readers take a connection (the board reads all its sections under one) and
# writers take a path, matching the rest of this module.


def enqueue_confession(
    db_path: Path,
    *,
    guild_id: int,
    author_id: int,
    content: str,
    notify_original_author: int,
    created_at: int | None = None,
) -> int:
    """Park a submission for review. Returns its queue id.

    ``created_at`` defaults to now, which is right for a fresh submission and
    wrong for a **requeue**. A confession put back after a failed post keeps its
    original timestamp: the seven-day sweep is a promise to the member, not a
    housekeeping heuristic, and a row that restamped itself on every retry would
    outlive that promise for as long as the failure lasted — a destination
    channel deleted from under an always-failing publish would keep a
    confession's text beside its author's id indefinitely. It also keeps the
    oldest-first queue honest, rather than sending a confession that has waited
    six days to the back of the line.
    """
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO confession_pending (
                guild_id, author_id, content, notify_original_author, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                guild_id, author_id, content, notify_original_author,
                now_ts() if created_at is None else int(created_at),
            ),
        )
        return int(cur.lastrowid or 0)


def pending_confessions(conn, guild_id: int, *, limit: int = 25) -> list[dict]:
    """The queue for one guild, oldest first — the order a mod should work it.

    No author id comes back. The board section and the picker are moderator
    surfaces, and moderator is a wider circle than the admin-only Confessions
    Audit Log, which is admin-gated *precisely* because it names the author.
    Approving a confession must not become a second way to de-anonymise one, so
    nothing on this path can print a name even by accident.
    """
    rows = conn.execute(
        """
        SELECT id, content, created_at
        FROM confession_pending
        WHERE guild_id = ?
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (guild_id, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_confession_count(conn, guild_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM confession_pending WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def get_pending_confession(conn, guild_id: int, pending_id: int) -> Optional[dict]:
    """One queued confession, for the review card. Still no author id."""
    row = conn.execute(
        """
        SELECT id, content, created_at
        FROM confession_pending
        WHERE guild_id = ? AND id = ?
        """,
        (guild_id, int(pending_id)),
    ).fetchone()
    return dict(row) if row else None


def resolve_pending_confession(
    db_path: Path, guild_id: int, pending_id: int
) -> Optional[dict]:
    """Claim a queued confession: delete it and hand back what it held.

    Returns ``None`` when the row is already gone — someone else resolved it, or
    the seven-day sweep took it. The delete and the read share one
    ``BEGIN IMMEDIATE`` so the claim is exactly-once; a caller that gets a row
    back is the only caller that will, and may safely post or DM on it.

    This is the *only* function that returns ``author_id``, because approving
    has to write the thread row and rejecting has to DM somebody. It is not a
    listing call and nothing renders its result.
    """
    with open_db_immediate(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, author_id, content, notify_original_author, created_at
            FROM confession_pending
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, int(pending_id)),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "DELETE FROM confession_pending WHERE guild_id = ? AND id = ?",
            (guild_id, int(pending_id)),
        )
        return dict(row)


def purge_expired_pending(
    db_path: Path, max_age_seconds: int = PENDING_TTL_SECONDS
) -> list[dict]:
    """Sweep confessions nobody reviewed in time, returning what was dropped.

    The rows come back so the caller can tell each author their confession
    expired — the same courtesy a rejection gets, since from the member's side
    the outcome is identical: it never appeared and they were never told why.
    """
    cutoff = now_ts() - max_age_seconds
    with open_db_immediate(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, guild_id, author_id, created_at
                FROM confession_pending
                WHERE created_at < ?
                """,
                (cutoff,),
            ).fetchall()
        ]
        if rows:
            conn.execute(
                "DELETE FROM confession_pending WHERE created_at < ?", (cutoff,)
            )
    return rows


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
