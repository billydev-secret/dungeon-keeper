"""Whisper cog — DB query layer (sync sqlite3)."""
from __future__ import annotations

import sqlite3

from db_utils import get_config_value, set_config_value
from services.whisper_models import WhisperConfig

_CONFIG_DEFAULTS: dict[str, str] = {
    "whisper_role_id": "0",
    "whisper_channel_id": "0",
    "whisper_log_channel_id": "0",
}


def get_whisper_config(conn: sqlite3.Connection, guild_id: int) -> WhisperConfig:
    def _get(key: str) -> str:
        return get_config_value(conn, key, _CONFIG_DEFAULTS[key], guild_id)

    return WhisperConfig(
        guild_id=guild_id,
        role_id=int(_get("whisper_role_id") or 0),
        channel_id=int(_get("whisper_channel_id") or 0),
        log_channel_id=int(_get("whisper_log_channel_id") or 0),
    )


def set_whisper_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """key is the full config key, e.g. 'whisper_channel_id'."""
    set_config_value(conn, key, value, guild_id)
