"""Storage helpers for external game-bot tracking.

The collector banks every watched-bot message RAW (content + embed dicts) keyed
on the source ``message_id`` so restarts, edits, and backfills all de-duplicate
instead of inflating counts. Metrics are derived from this table later, never at
capture time — see ``parser.py`` (added once the raw format is confirmed).
"""
from __future__ import annotations

import json
from typing import Any, Mapping

import discord


# Parser kinds a watch can carry — each selects a parser + economy mapping in
# parser.py. Ordered for display; the first is the default a bare migration row
# takes. Labels are what /games track shows.
WATCH_KIND_LABELS: dict[str, str] = {
    "gamebot_cah": "Gamebot (Cards Against Humanity)",
    "catbot": "Cat Bot",
}
VALID_WATCH_KINDS: tuple[str, ...] = tuple(WATCH_KIND_LABELS)


async def list_watches(db, guild_id: int) -> list[Mapping[str, Any]]:
    """Every watch config for a guild (enabled or paused), newest first."""
    rows = await db.fetchall(
        "SELECT id, guild_id, channel_id, bot_user_id, kind, enabled "
        "FROM games_external_watch WHERE guild_id = ? ORDER BY set_at DESC",
        (guild_id,),
    )
    return list(rows)


async def get_watch_for_bot(
    db, guild_id: int, bot_user_id: int
) -> Mapping[str, Any] | None:
    """The watch row for one (guild, bot), or None."""
    return await db.fetchone(
        "SELECT id, guild_id, channel_id, bot_user_id, kind, enabled "
        "FROM games_external_watch WHERE guild_id = ? AND bot_user_id = ?",
        (guild_id, bot_user_id),
    )


async def set_watch(
    db, guild_id: int, channel_id: int, bot_user_id: int, kind: str, set_by: int
) -> None:
    """Point a guild's collector at a channel + external bot (enabled).

    Idempotent per (guild, bot): re-running for the same bot repoints its
    channel/kind rather than adding a duplicate. Different bots coexist.
    """
    await db.execute(
        """
        INSERT INTO games_external_watch
            (guild_id, channel_id, bot_user_id, kind, enabled, set_by)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(guild_id, bot_user_id) DO UPDATE SET
            channel_id  = excluded.channel_id,
            kind        = excluded.kind,
            enabled     = 1,
            set_by      = excluded.set_by,
            set_at      = CURRENT_TIMESTAMP
        """,
        (guild_id, channel_id, bot_user_id, kind, set_by),
    )


async def set_watch_enabled(
    db, guild_id: int, bot_user_id: int, enabled: bool
) -> bool:
    """Toggle one bot's collection on/off. False if no such watch exists."""
    cur = await db.execute(
        "UPDATE games_external_watch SET enabled = ? "
        "WHERE guild_id = ? AND bot_user_id = ?",
        (1 if enabled else 0, guild_id, bot_user_id),
    )
    return cur.rowcount > 0


async def load_all_watches(db) -> list[Mapping[str, Any]]:
    """All enabled watch configs, for warming the in-memory cache on startup."""
    rows = await db.fetchall(
        "SELECT guild_id, channel_id, bot_user_id, kind FROM games_external_watch "
        "WHERE enabled = 1"
    )
    return list(rows)


def message_to_row(message: discord.Message) -> tuple:
    """Flatten a discord Message into the games_external_messages column order."""
    embeds_json = json.dumps([e.to_dict() for e in message.embeds])
    edited = message.edited_at.isoformat() if message.edited_at else None
    return (
        message.id,
        message.guild.id if message.guild else 0,
        message.channel.id,
        message.author.id,
        message.created_at.isoformat(),
        edited,
        message.content or "",
        embeds_json,
    )


async def store_message(db, message: discord.Message) -> None:
    """Idempotently bank a raw message. Re-capturing (e.g. on edit) refreshes
    content/embeds and clears any prior parse so the parser revisits it."""
    row = message_to_row(message)
    await db.execute(
        """
        INSERT INTO games_external_messages
            (message_id, guild_id, channel_id, author_id, created_at,
             edited_at, content, embeds_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            edited_at    = excluded.edited_at,
            content      = excluded.content,
            embeds_json  = excluded.embeds_json,
            parse_status = NULL,
            parsed_at    = NULL
        """,
        row,
    )


async def claim_payout(db, message_id: int, guild_id: int, kind: str) -> bool:
    """Reserve the one-time payout for a terminal message. True on first claim.

    Backs the "pay each external game exactly once" guarantee independently of
    ``parse_status`` (which edits reset). A second caller for the same message
    gets False and must not pay.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO games_external_payouts (message_id, guild_id, kind) "
        "VALUES (?, ?, ?)",
        (message_id, guild_id, kind),
    )
    return cur.rowcount > 0


async def recent_channel_messages(
    db, guild_id: int, channel_id: int, author_id: int, before_iso: str, limit: int = 300
) -> list[Mapping[str, Any]]:
    """Banked messages for one (guild, channel, bot) at//before a timestamp,
    oldest-first — the window a parser walks to reconstruct a finished game."""
    rows = await db.fetchall(
        "SELECT message_id, created_at, content, embeds_json FROM games_external_messages "
        "WHERE guild_id = ? AND channel_id = ? AND author_id = ? AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (guild_id, channel_id, author_id, before_iso, limit),
    )
    return list(reversed(list(rows)))


async def mark_parsed(db, message_id: int, status: str) -> None:
    """Stamp a banked message's parse outcome ('ok' | 'skip' | 'error')."""
    await db.execute(
        "UPDATE games_external_messages SET parse_status = ?, "
        "parsed_at = CURRENT_TIMESTAMP WHERE message_id = ?",
        (status, message_id),
    )


async def count_messages(db, guild_id: int, bot_user_id: int | None = None) -> int:
    """How many raw messages we've banked for a guild (optionally one bot)."""
    if bot_user_id is None:
        r = await db.fetchone(
            "SELECT COUNT(*) AS n FROM games_external_messages WHERE guild_id = ?",
            (guild_id,),
        )
    else:
        r = await db.fetchone(
            "SELECT COUNT(*) AS n FROM games_external_messages "
            "WHERE guild_id = ? AND author_id = ?",
            (guild_id, bot_user_id),
        )
    return int(r["n"]) if r else 0
