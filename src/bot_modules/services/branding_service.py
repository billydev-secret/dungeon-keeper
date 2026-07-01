"""Per-guild branding config — the embed accent colour.

Bot identity (per-guild nickname + avatar) is handled directly against
Discord by the web route ``POST /config/bot-identity`` and lives on the
Discord member, not here. This table only stores the accent-colour
preference used by the shared resolver in ``bot_modules.core.branding``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import open_db

# Discord blurple — the safe default accent when nothing else resolves.
DEFAULT_ACCENT = 0x5865F2

ACCENT_MODE_AVATAR = "avatar"
ACCENT_MODE_CUSTOM = "custom"
_VALID_MODES = frozenset({ACCENT_MODE_AVATAR, ACCENT_MODE_CUSTOM})

# accent_hex sentinel meaning "no custom colour set".
ACCENT_HEX_UNSET = -1


@dataclass
class BrandingConfig:
    guild_id: int
    accent_mode: str = ACCENT_MODE_AVATAR
    accent_hex: int = ACCENT_HEX_UNSET  # -1 = unset; else 0x000000..0xFFFFFF

    def normalized_mode(self) -> str:
        return self.accent_mode if self.accent_mode in _VALID_MODES else ACCENT_MODE_AVATAR

    def has_custom_colour(self) -> bool:
        return 0 <= self.accent_hex <= 0xFFFFFF


def init_db(db_path: Path) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS branding_config (
            guild_id    INTEGER PRIMARY KEY,
            accent_mode TEXT NOT NULL DEFAULT 'avatar',
            accent_hex  INTEGER NOT NULL DEFAULT -1
        )
        """
    )


def _row_to_cfg(row) -> BrandingConfig:
    return BrandingConfig(
        guild_id=row["guild_id"],
        accent_mode=row["accent_mode"],
        accent_hex=row["accent_hex"],
    )


def get_branding(db_path: Path, guild_id: int) -> BrandingConfig:
    with open_db(db_path) as conn:
        return get_branding_conn(conn, guild_id)


def get_branding_conn(conn, guild_id: int) -> BrandingConfig:
    """Read branding config on an open connection.

    Defensive against a missing table (returns defaults) so a bot that
    posts an embed before the table has been created never crashes.
    """
    try:
        row = conn.execute(
            "SELECT guild_id, accent_mode, accent_hex FROM branding_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return BrandingConfig(guild_id=guild_id)
    if row is None:
        return BrandingConfig(guild_id=guild_id)
    return _row_to_cfg(row)


def upsert_branding(db_path: Path, cfg: BrandingConfig) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)
        conn.execute(
            """
            INSERT INTO branding_config (guild_id, accent_mode, accent_hex)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                accent_mode = excluded.accent_mode,
                accent_hex  = excluded.accent_hex
            """,
            (cfg.guild_id, cfg.normalized_mode(), cfg.accent_hex),
        )
