"""Admin-confirmed config changes proposed by Billy-bot.

Billy-bot can *propose* a settings change via its ``propose_config_change``
tool, but never applies one itself: the proposal is validated here, attached
to the reply as an Apply button, and only written when an admin clicks it
(``advisor_cog``). That human gate is the prompt-injection defence — pinned
messages, server docs, and announcements are all in the model's context, so
model output alone must never mutate config.

Scope is deliberately narrow for v1: only keys in the shared ``config`` KV
table, and only keys that already exist for the guild. Feature-table settings
(economy prices, voice master, …) have their own validated dashboard panels
and stay read-only to the model.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from bot_modules.core.db_utils import open_db, set_config_value

log = logging.getLogger(__name__)

# Same secret pattern as the read side (advisor_context) — never touch these.
_SECRET_KEY_RE = re.compile(
    r"token|secret|refresh|password|passwd|api[_-]?key|webhook|oauth|credential",
    re.I,
)
_MAX_VALUE_CHARS = 200
_CLEAR_WORDS = {"none", "off", "clear", "unset", "0"}
_TRUE_WORDS = {"1", "on", "true", "yes", "enable", "enabled"}
_FALSE_WORDS = {"0", "off", "false", "no", "disable", "disabled"}


@dataclass(frozen=True)
class ConfigProposal:
    """A validated, not-yet-applied change to one config KV key."""

    key: str
    value: str  # normalized, exactly as it would be stored
    display: str  # human-readable, e.g. "welcome_channel_id → #welcome"


def _current_value(conn: sqlite3.Connection, guild_id: int, key: str) -> str | None:
    """The key's stored value, honouring the same legacy guild-0 fallback reads use."""
    for gid in (guild_id, 0):
        row = conn.execute(
            "SELECT value FROM config WHERE guild_id = ? AND key = ?", (gid, key)
        ).fetchone()
        if row is not None:
            return str(row["value"])
    return None


def _resolve_channel(guild, raw: str) -> tuple[str, str]:
    """'<#id>' / id / '#name' / name → (id-as-str, '#name'). Raises ValueError."""
    s = raw.strip()
    m = re.fullmatch(r"<#(\d+)>|#?(\d{5,})", s)
    if m:
        ch = guild.get_channel(int(m.group(1) or m.group(2)))
        if ch is not None:
            return str(ch.id), f"#{ch.name}"
        raise ValueError(f"no channel with id {m.group(1) or m.group(2)} in this server")
    name = s.lstrip("#").casefold()
    for ch in getattr(guild, "text_channels", []):
        if ch.name.casefold() == name:
            return str(ch.id), f"#{ch.name}"
    raise ValueError(f"no channel named '{s}' in this server")


def _resolve_role(guild, raw: str) -> tuple[str, str]:
    """'<@&id>' / id / '@name' / name → (id-as-str, '@name'). Raises ValueError."""
    s = raw.strip()
    m = re.fullmatch(r"<@&(\d+)>|@?(\d{5,})", s)
    if m:
        role = guild.get_role(int(m.group(1) or m.group(2)))
        if role is not None:
            return str(role.id), f"@{role.name}"
        raise ValueError(f"no role with id {m.group(1) or m.group(2)} in this server")
    name = s.lstrip("@").casefold()
    for role in getattr(guild, "roles", []):
        if getattr(role, "name", "").casefold() == name:
            return str(role.id), f"@{role.name}"
    raise ValueError(f"no role named '{s}' in this server")


def validate_config_change(
    conn: sqlite3.Connection, guild, key: str, raw_value: str
) -> ConfigProposal:
    """Validate one proposed change; return the normalized proposal.

    Raises ``ValueError`` with a model-readable reason on anything off —
    unknown key, secret key, unresolvable channel/role, bad boolean/number.
    The value's expected shape is inferred from the key suffix and the
    currently stored value, mirroring how ``_fmt_value`` reads them.
    """
    key = (key or "").strip()
    raw = (raw_value or "").strip()
    if not key or not raw:
        raise ValueError("both key and value are required")
    if _SECRET_KEY_RE.search(key):
        raise ValueError("that key can't be changed here")
    if len(raw) > _MAX_VALUE_CHARS:
        raise ValueError(f"value too long (max {_MAX_VALUE_CHARS} chars)")

    current = _current_value(conn, guild.id, key)
    if current is None:
        raise ValueError(
            f"'{key}' isn't a saved setting on this server — I can only change "
            "settings that already exist. Point the admin to the feature's "
            "dashboard panel to set it up first."
        )

    low_key = key.lower()
    low_raw = raw.casefold()
    if low_key.endswith(("channel_id", "channel")):
        if low_raw in _CLEAR_WORDS:
            return ConfigProposal(key, "0", f"{key} → (cleared)")
        value, shown = _resolve_channel(guild, raw)
        return ConfigProposal(key, value, f"{key} → {shown}")
    if low_key.endswith(("role_id", "role")):
        if low_raw in _CLEAR_WORDS:
            return ConfigProposal(key, "0", f"{key} → (cleared)")
        value, shown = _resolve_role(guild, raw)
        return ConfigProposal(key, value, f"{key} → {shown}")
    if current in ("0", "1"):
        if low_raw in _TRUE_WORDS:
            return ConfigProposal(key, "1", f"{key} → on")
        if low_raw in _FALSE_WORDS:
            return ConfigProposal(key, "0", f"{key} → off")
        raise ValueError(f"'{key}' is an on/off setting — say on or off")
    if current.lstrip("-").isdigit():
        try:
            num = int(raw.replace(",", ""))
        except ValueError:
            raise ValueError(f"'{key}' expects a whole number") from None
        return ConfigProposal(key, str(num), f"{key} → {num}")
    return ConfigProposal(key, raw, f"{key} → {raw}")


def apply_config_change(db_path, guild, proposal: ConfigProposal) -> None:
    """Write one confirmed proposal. Re-validates so a stale button can't
    apply a change that stopped making sense (channel deleted, key removed)."""
    with open_db(db_path) as conn:
        checked = validate_config_change(conn, guild, proposal.key, proposal.value)
        set_config_value(conn, checked.key, checked.value, guild.id)
    log.info(
        "advisor applied config change for guild %s: %s = %s",
        guild.id, checked.key, checked.value,
    )
