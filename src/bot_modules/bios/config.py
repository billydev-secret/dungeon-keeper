"""Per-guild bios config snapshot — mirrors the GuildConfig pattern.

Reads through `get_config_value` so empty/unset keys fall back to spec
defaults. Loaded inside `_start_or_resume` to gate the wizard on a
sane configuration (channel + category + at least one active field).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from bot_modules.core.db_utils import get_config_value
from bot_modules.services.embeds import BIOS_PRIMARY


@dataclass(frozen=True)
class BiosConfig:
    """Immutable snapshot of one guild's bios config."""

    guild_id: int
    bios_channel_id: int
    wizard_category_id: int
    questions_per_bio: int
    embed_color: int
    wizard_timeout_minutes: int
    archive_grace_seconds: int

    @property
    def configured(self) -> bool:
        """True when both the bios channel and wizard category are set."""
        return self.bios_channel_id > 0 and self.wizard_category_id > 0

    @classmethod
    def load(cls, conn: sqlite3.Connection, guild_id: int) -> "BiosConfig":
        """Read the six config keys; missing → spec defaults."""

        def _val(key: str, default: str = "") -> str:
            return get_config_value(conn, key, default, guild_id)

        def _int(key: str, default: int) -> int:
            raw = _val(key, str(default))
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        def _color(key: str, default: int) -> int:
            raw = _val(key, "").strip()
            if not raw:
                return default
            raw = raw.lstrip("#")
            try:
                return int(raw, 16) if len(raw) <= 8 else default
            except ValueError:
                try:
                    return int(raw)
                except ValueError:
                    return default

        return cls(
            guild_id=guild_id,
            bios_channel_id=_int("bios_channel_id", 0),
            wizard_category_id=_int("bios_wizard_category_id", 0),
            questions_per_bio=max(1, _int("bios_questions_per_bio", 3)),
            embed_color=_color("bios_embed_color", BIOS_PRIMARY),
            wizard_timeout_minutes=max(1, _int("bios_wizard_timeout", 15)),
            archive_grace_seconds=max(0, _int("bios_archive_grace", 60)),
        )
