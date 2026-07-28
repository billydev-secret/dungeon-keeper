from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from bot_modules.core.db_utils import open_db


def parse_emojis(emoji_str: str) -> list[str]:
    return [e.strip() for e in emoji_str.split(",") if e.strip()]


def upsert_auto_react_rule(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    emojis: list[str],
    enabled: bool = True,
    tips_enabled: bool = False,
) -> None:
    emoji_str = ",".join(emojis)
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO auto_react_config
                (guild_id, channel_id, emojis, enabled, tips_enabled)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, channel_id) DO UPDATE SET
                emojis       = excluded.emojis,
                enabled      = excluded.enabled,
                tips_enabled = excluded.tips_enabled
            """,
            (guild_id, channel_id, emoji_str, int(enabled), int(tips_enabled)),
        )


def remove_auto_react_rule(db_path: Path, guild_id: int, channel_id: int) -> bool:
    with open_db(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM auto_react_config WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        )
        return cursor.rowcount > 0


def list_auto_react_rules_for_guild_with_conn(
    conn: sqlite3.Connection, guild_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT channel_id, emojis, enabled, tips_enabled FROM auto_react_config "
        "WHERE guild_id=? ORDER BY channel_id",
        (guild_id,),
    ).fetchall()


def list_auto_react_rules_for_guild(db_path: Path, guild_id: int) -> list[sqlite3.Row]:
    with open_db(db_path) as conn:
        return list_auto_react_rules_for_guild_with_conn(conn, guild_id)


def should_place_tip_emoji(
    *,
    channel_is_nsfw: bool,
    verdicts: list[bool | None],
) -> bool:
    """Whether a tipping rule should place its emoji on this post.

    A bot-placed emoji in a tipping channel *is* a live payment button, so it
    may only go on a post that qualified: the channel must be age-gated (the
    rail CLAUDE.md requires — a classifier is a bot-side judgment call and
    never substitutes for Discord's own gate), and at least one attachment
    must not have been ruled out.

    Fails open. An unreadable image (``None``) still gets emoji, because the
    cost of being wrong here is a poster silently losing their tips over a CDN
    hiccup, which they have no way to see or appeal. Only a confident "read it,
    not explicit" (``False``) withholds them.

    *verdicts* covers attachments only — embeds are never classified, so a
    tipping channel places nothing on them (see nsfw_classifier_spec.md).
    """
    if not channel_is_nsfw:
        return False
    return any(verdict is not False for verdict in verdicts)


def record_placement(
    db_path: Path,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_id: int,
    emojis: list[str],
    now: int | None = None,
) -> None:
    """Record what the bot placed — the receipt that makes a rung tippable."""
    created_at = int(time.time()) if now is None else now
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO auto_react_placements
                (guild_id, channel_id, message_id, author_id, emojis, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, message_id, author_id, ",".join(emojis), created_at),
        )


def get_placement(
    db_path: Path, guild_id: int, message_id: int
) -> sqlite3.Row | None:
    with open_db(db_path) as conn:
        return get_placement_with_conn(conn, guild_id, message_id)


def get_placement_with_conn(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT channel_id, message_id, author_id, emojis FROM auto_react_placements "
        "WHERE guild_id=? AND message_id=?",
        (guild_id, message_id),
    ).fetchone()


def get_auto_react_rule(
    db_path: Path, guild_id: int, channel_id: int
) -> sqlite3.Row | None:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT channel_id, emojis, enabled, tips_enabled FROM auto_react_config "
            "WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ).fetchone()
