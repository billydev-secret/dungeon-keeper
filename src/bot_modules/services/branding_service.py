"""Per-guild branding config — the embed accent color and product names.

Bot identity (per-guild nickname + avatar) is handled directly against
Discord by the web route ``POST /config/bot-identity`` and lives on the
Discord member, not here. This table stores the accent-color preference
used by the shared resolver in ``bot_modules.core.branding``, plus the
guild-facing names of the two named products — the casino and the AI
assistant. Those names default to the values the home server has always
used; every other guild can rename them from the Branding panel.

This module is the single home for those defaults: call sites that have no
guild handy import ``DEFAULT_CASINO_NAME`` / ``DEFAULT_ASSISTANT_NAME``
from here rather than repeating the literal.
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

# accent_hex sentinel meaning "no custom color set".
ACCENT_HEX_UNSET = -1

# Built-in product names. A guild that never sets its own reads these.
DEFAULT_CASINO_NAME = "Golden Meadow"
DEFAULT_ASSISTANT_NAME = "Billy-bot"

# Names are rendered into embed titles and slash-command replies; keep them
# short enough that a title can never blow Discord's 256-char limit.
MAX_NAME_LEN = 60


@dataclass
class BrandingConfig:
    guild_id: int
    accent_mode: str = ACCENT_MODE_AVATAR
    accent_hex: int = ACCENT_HEX_UNSET  # -1 = unset; else 0x000000..0xFFFFFF
    # "" (stored NULL) = fall back to the module default above.
    casino_name: str = ""
    assistant_name: str = ""

    def normalized_mode(self) -> str:
        return self.accent_mode if self.accent_mode in _VALID_MODES else ACCENT_MODE_AVATAR

    def has_custom_color(self) -> bool:
        return 0 <= self.accent_hex <= 0xFFFFFF

    def resolved_casino_name(self) -> str:
        return (self.casino_name or "").strip() or DEFAULT_CASINO_NAME

    def resolved_assistant_name(self) -> str:
        return (self.assistant_name or "").strip() or DEFAULT_ASSISTANT_NAME


def init_db(db_path: Path) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create the table on a fresh DB — kept in sync with migration 115.

    The name columns are added with ALTER so a database whose table predates
    migration 115 (created lazily by an older build of this module) still ends
    up with the same shape as a migrated one.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS branding_config (
            guild_id       INTEGER PRIMARY KEY,
            accent_mode    TEXT NOT NULL DEFAULT 'avatar',
            accent_hex     INTEGER NOT NULL DEFAULT -1,
            casino_name    TEXT,
            assistant_name TEXT
        )
        """
    )
    for column in ("casino_name", "assistant_name"):
        try:
            conn.execute(f"ALTER TABLE branding_config ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass  # already present


def _row_to_cfg(row) -> BrandingConfig:
    return BrandingConfig(
        guild_id=row["guild_id"],
        accent_mode=row["accent_mode"],
        accent_hex=row["accent_hex"],
        casino_name=row["casino_name"] or "",
        assistant_name=row["assistant_name"] or "",
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
            "SELECT guild_id, accent_mode, accent_hex, casino_name, assistant_name "
            "FROM branding_config WHERE guild_id = ?",
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
            INSERT INTO branding_config
                (guild_id, accent_mode, accent_hex, casino_name, assistant_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                accent_mode    = excluded.accent_mode,
                accent_hex     = excluded.accent_hex,
                casino_name    = excluded.casino_name,
                assistant_name = excluded.assistant_name
            """,
            (
                cfg.guild_id,
                cfg.normalized_mode(),
                cfg.accent_hex,
                _stored_name(cfg.casino_name),
                _stored_name(cfg.assistant_name),
            ),
        )


def _stored_name(raw: str | None) -> str | None:
    """Trim a name for storage; blank (or default-equal) becomes NULL."""
    name = (raw or "").strip()[:MAX_NAME_LEN]
    return name or None


# ── name resolvers ─────────────────────────────────────────────────────
#
# Thin wrappers so a call site can ask for "this guild's casino name" without
# knowing about the config row or repeating the default literal.


def resolve_casino_name(db_path: Path, guild_id: int) -> str:
    return get_branding(db_path, guild_id).resolved_casino_name()


def resolve_casino_name_conn(conn, guild_id: int) -> str:
    return get_branding_conn(conn, guild_id).resolved_casino_name()


def resolve_assistant_name(db_path: Path, guild_id: int) -> str:
    return get_branding(db_path, guild_id).resolved_assistant_name()


def resolve_assistant_name_conn(conn, guild_id: int) -> str:
    return get_branding_conn(conn, guild_id).resolved_assistant_name()
