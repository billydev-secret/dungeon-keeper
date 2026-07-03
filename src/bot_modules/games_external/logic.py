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


async def get_watch(db, guild_id: int) -> Mapping[str, Any] | None:
    """Return the watch config row for a guild, or None if unset."""
    return await db.fetchone(
        "SELECT guild_id, channel_id, bot_user_id, enabled FROM games_external_watch "
        "WHERE guild_id = ?",
        (guild_id,),
    )


async def set_watch(
    db, guild_id: int, channel_id: int, bot_user_id: int, set_by: int
) -> None:
    """Point a guild's collector at a channel + external bot (enabled)."""
    await db.execute(
        """
        INSERT INTO games_external_watch (guild_id, channel_id, bot_user_id, enabled, set_by)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            channel_id  = excluded.channel_id,
            bot_user_id = excluded.bot_user_id,
            enabled     = 1,
            set_by      = excluded.set_by,
            set_at      = CURRENT_TIMESTAMP
        """,
        (guild_id, channel_id, bot_user_id, set_by),
    )


async def set_watch_enabled(db, guild_id: int, enabled: bool) -> bool:
    """Toggle collection on/off. Returns False if no watch config exists yet."""
    cur = await db.execute(
        "UPDATE games_external_watch SET enabled = ? WHERE guild_id = ?",
        (1 if enabled else 0, guild_id),
    )
    return cur.rowcount > 0


async def load_all_watches(db) -> list[Mapping[str, Any]]:
    """All enabled watch configs, for warming the in-memory cache on startup."""
    rows = await db.fetchall(
        "SELECT guild_id, channel_id, bot_user_id FROM games_external_watch "
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


async def count_messages(db, guild_id: int) -> int:
    """How many raw messages we've banked for a guild."""
    r = await db.fetchone(
        "SELECT COUNT(*) AS n FROM games_external_messages WHERE guild_id = ?",
        (guild_id,),
    )
    return int(r["n"]) if r else 0
