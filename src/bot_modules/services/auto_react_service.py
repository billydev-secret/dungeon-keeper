from __future__ import annotations

import sqlite3
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
) -> None:
    emoji_str = ",".join(emojis)
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO auto_react_config (guild_id, channel_id, emojis, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, channel_id) DO UPDATE SET
                emojis  = excluded.emojis,
                enabled = excluded.enabled
            """,
            (guild_id, channel_id, emoji_str, int(enabled)),
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
        "SELECT channel_id, emojis, enabled FROM auto_react_config WHERE guild_id=? ORDER BY channel_id",
        (guild_id,),
    ).fetchall()


def list_auto_react_rules_for_guild(db_path: Path, guild_id: int) -> list[sqlite3.Row]:
    with open_db(db_path) as conn:
        return list_auto_react_rules_for_guild_with_conn(conn, guild_id)


def get_auto_react_rule(
    db_path: Path, guild_id: int, channel_id: int
) -> sqlite3.Row | None:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT channel_id, emojis, enabled FROM auto_react_config WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ).fetchone()
