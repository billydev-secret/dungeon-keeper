"""Music cog — DB layer for per-voice-channel settings (24/7 + autoplay).

Sync sqlite via shared db_utils.open_db() (matches project convention).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelSettings:
    guild_id: int
    voice_channel_id: int
    always_on: bool
    autoplay_playlist_url: str | None
    last_updated_ts: int
    updated_by_user_id: int


def _row_to_settings(row: sqlite3.Row) -> ChannelSettings:
    return ChannelSettings(
        guild_id=row["guild_id"],
        voice_channel_id=row["voice_channel_id"],
        always_on=bool(row["always_on"]),
        autoplay_playlist_url=row["autoplay_playlist_url"],
        last_updated_ts=row["last_updated_ts"],
        updated_by_user_id=row["updated_by_user_id"],
    )


def get_channel_settings(
    conn: sqlite3.Connection, guild_id: int, voice_channel_id: int
) -> ChannelSettings | None:
    row = conn.execute(
        "SELECT * FROM music_channel_settings WHERE guild_id = ? AND voice_channel_id = ?",
        (guild_id, voice_channel_id),
    ).fetchone()
    return _row_to_settings(row) if row else None


def set_always_on(
    conn: sqlite3.Connection,
    guild_id: int,
    voice_channel_id: int,
    enabled: bool,
    user_id: int,
) -> None:
    """Toggle 24/7 for a voice channel.

    v1 invariant: at most one 24/7 channel per guild. Enabling on a new
    channel atomically clears always_on on every other channel in the same guild
    (their autoplay URL is preserved, just disabled). Disabling only touches
    the named channel.
    """
    now = int(time.time())
    if enabled:
        # Clear other always_on rows in this guild (preserve their autoplay URL).
        conn.execute(
            "UPDATE music_channel_settings "
            "SET always_on = 0, last_updated_ts = ?, updated_by_user_id = ? "
            "WHERE guild_id = ? AND voice_channel_id != ? AND always_on = 1",
            (now, user_id, guild_id, voice_channel_id),
        )
    conn.execute(
        """
        INSERT INTO music_channel_settings
            (guild_id, voice_channel_id, always_on, autoplay_playlist_url,
             last_updated_ts, updated_by_user_id)
        VALUES (?, ?, ?, NULL, ?, ?)
        ON CONFLICT(guild_id, voice_channel_id) DO UPDATE SET
            always_on = excluded.always_on,
            last_updated_ts = excluded.last_updated_ts,
            updated_by_user_id = excluded.updated_by_user_id
        """,
        (guild_id, voice_channel_id, 1 if enabled else 0, now, user_id),
    )


def set_autoplay_playlist(
    conn: sqlite3.Connection,
    guild_id: int,
    voice_channel_id: int,
    playlist_url: str | None,
    user_id: int,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO music_channel_settings
            (guild_id, voice_channel_id, always_on, autoplay_playlist_url,
             last_updated_ts, updated_by_user_id)
        VALUES (?, ?, 0, ?, ?, ?)
        ON CONFLICT(guild_id, voice_channel_id) DO UPDATE SET
            autoplay_playlist_url = excluded.autoplay_playlist_url,
            last_updated_ts = excluded.last_updated_ts,
            updated_by_user_id = excluded.updated_by_user_id
        """,
        (guild_id, voice_channel_id, playlist_url, now, user_id),
    )


def list_always_on_channels(
    conn: sqlite3.Connection, guild_id: int
) -> list[ChannelSettings]:
    rows = conn.execute(
        "SELECT * FROM music_channel_settings WHERE guild_id = ? AND always_on = 1",
        (guild_id,),
    ).fetchall()
    return [_row_to_settings(r) for r in rows]


def list_all_always_on(conn: sqlite3.Connection) -> list[ChannelSettings]:
    rows = conn.execute(
        "SELECT * FROM music_channel_settings WHERE always_on = 1"
    ).fetchall()
    return [_row_to_settings(r) for r in rows]


def clear_channel(
    conn: sqlite3.Connection, guild_id: int, voice_channel_id: int
) -> bool:
    cur = conn.execute(
        "DELETE FROM music_channel_settings WHERE guild_id = ? AND voice_channel_id = ?",
        (guild_id, voice_channel_id),
    )
    return (cur.rowcount or 0) > 0
