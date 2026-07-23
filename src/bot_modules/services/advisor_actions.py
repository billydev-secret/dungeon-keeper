"""Admin-confirmed config changes proposed by Billy-bot.

Billy-bot can *propose* a settings change via its ``propose_config_change``
tool, but never applies one itself: the proposal is validated here, attached
to the reply as an Apply button, and only written when an admin clicks it
(``advisor_cog``). That human gate is the prompt-injection defence — pinned
messages, server docs, and announcements are all in the model's context, so
model output alone must never mutate config.

Scope comes from ``settings_registry``: a key is proposable only if it is listed
there *and* flagged ``writable``. That is a deliberate narrowing of the old rule
("any non-secret key that already has a row"), which let the model reach keys
nobody had vetted — including ``admin_role_ids`` and ``message_storage_level``.
Feature-table settings (economy prices, voice master dials, …) have their own
validated dashboard panels and stay read-only to the model.

The registry also removes the old requirement that the key already exist. Shape
now comes from the schema rather than from the stored value, so Billy-bot can
set up a feature that has never been configured — the case adoption depends on.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.settings_registry import (
    Setting,
    coerce_value,
    feature_for_key,
    get_setting,
)

log = logging.getLogger(__name__)

# Same secret pattern as the read side (advisor_context) — never touch these.
# The registry rejects secret-shaped keys at import; this is belt-and-braces for
# a caller that reaches validate_config_change with something unexpected.
_SECRET_KEY_RE = re.compile(
    r"token|secret|refresh|password|passwd|api[_-]?key|webhook|oauth|credential",
    re.I,
)
_MAX_VALUE_CHARS = 200
_CLEAR_WORDS = {"none", "off", "clear", "unset", "0"}


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


def _resolve_setting(key: str, *, is_admin: bool) -> Setting:
    """The schema for a proposable key, or a ValueError explaining why not."""
    setting = get_setting(key)
    if setting is None:
        raise ValueError(
            f"'{key}' isn't a setting I can change. I can only change settings "
            "on my vetted list — point the admin to the feature's dashboard "
            "panel instead."
        )
    if not setting.writable:
        feature = feature_for_key(key)
        where = f" — set it from {feature.panel}" if feature else ""
        raise ValueError(f"'{key}' has to be changed on the dashboard{where}.")
    if setting.admin_only and not is_admin:
        raise ValueError(
            f"'{key}' grants access or authority, so it needs a full server "
            "administrator — Manage Server isn't enough. Ask an admin, or set "
            "it from the dashboard."
        )
    return setting


def validate_config_change(
    conn: sqlite3.Connection,
    guild,
    key: str,
    raw_value: str,
    *,
    allow_noop: bool = False,
    is_admin: bool = False,
) -> ConfigProposal:
    """Validate one proposed change; return the normalized proposal.

    Shape comes from ``settings_registry``, so a key that has never been set on
    this server is still proposable — that's how Billy-bot can stand up an
    unconfigured feature. Raises ``ValueError`` with a model-readable reason on
    anything off: unlisted or panel-only key, unresolvable channel/role, bad
    boolean/number/choice, or a change that wouldn't change anything.

    ``allow_noop`` skips the last check. Re-validation at apply time passes it,
    so clicking a button whose value is already stored is a harmless rewrite
    rather than a confusing "no change needed" failure.

    ``is_admin`` is full ``administrator``, required for ``admin_only``
    settings. It defaults to False so a caller that forgets to thread it
    through fails closed, and it is re-checked at apply time against whoever
    clicks — not just whoever asked.
    """
    key = (key or "").strip()
    raw = (raw_value or "").strip()
    if not key or not raw:
        raise ValueError("both key and value are required")
    if _SECRET_KEY_RE.search(key):
        raise ValueError("that key can't be changed here")
    if len(raw) > _MAX_VALUE_CHARS:
        raise ValueError(f"value too long (max {_MAX_VALUE_CHARS} chars)")

    setting = _resolve_setting(key, is_admin=is_admin)
    label = setting.label
    low_raw = raw.casefold()

    if setting.kind in ("channel", "role") and low_raw in _CLEAR_WORDS:
        value, shown = "0", "(cleared)"
    elif setting.kind == "channel":
        value, shown = _resolve_channel(guild, raw)
    elif setting.kind == "role":
        value, shown = _resolve_role(guild, raw)
    else:
        value = coerce_value(setting, raw)
        shown = ("on" if value == "1" else "off") if setting.kind == "bool" else value

    if not allow_noop and _current_value(conn, guild.id, key) == value:
        raise ValueError(f"{label} is already set to {shown} — no change needed.")
    return ConfigProposal(key, value, f"{label} → {shown}")


def apply_config_change(
    db_path, guild, proposal: ConfigProposal, *, is_admin: bool = False
) -> None:
    """Write one confirmed proposal.

    Re-validates so a stale button can't apply a change that stopped making
    sense (channel deleted, key removed) — and so ``admin_only`` is enforced
    against the person who actually clicked, which may not be the person who
    asked. Defaults to non-admin so a caller that forgets fails closed.
    """
    with open_db(db_path) as conn:
        checked = validate_config_change(
            conn, guild, proposal.key, proposal.value,
            allow_noop=True, is_admin=is_admin,
        )
        set_config_value(conn, checked.key, checked.value, guild.id)
    log.info(
        "advisor applied config change for guild %s: %s = %s",
        guild.id, checked.key, checked.value,
    )
