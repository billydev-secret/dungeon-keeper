"""Per-asker server context for Billy-bot.

Billy-bot's static grounding is the user manual (``advisor_service``). This
module adds *live, per-server* grounding — server docs, recent announcements,
channel topics, and pinned messages — plus a summary of what the asker can
**do** (their roles/permissions), so answers are tailored to them.

Two hard rules, both enforced here and covered by tests:

- **See:** a channel's topic/pins are only ever included if the asker can view
  that channel (``permissions_for(viewer).view_channel``), and NSFW channels
  (``is_nsfw()``) are never included. ``/ask`` is open to everyone, so this is
  the gate that stops a member extracting mod-only content.
- **Do:** the capability summary reflects the asker's real permissions, so
  Billy-bot only suggests actions they can actually perform.

Pins require an API call per channel, so they come from a per-guild snapshot
refreshed by ``guild_pins_loop`` rather than fetched on every ``/ask``. The
snapshot holds every non-NSFW channel the *bot* can read; the per-asker
visibility gate is applied when the context is built, not when it's cached.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import re
import sqlite3
import time
from collections.abc import Callable

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.docs.db import list_docs
from bot_modules.services.announcements_service import list_announcements

log = logging.getLogger(__name__)

# Caps — keep the live block small so the cached manual stays the bulk of the
# prompt and per-ask cost stays bounded.
MAX_CHANNELS = 60
MAX_PINS_PER_CHANNEL = 5
MAX_PIN_CHARS = 300
MAX_TOPIC_CHARS = 300
MAX_DOCS = 12
MAX_DOC_CHARS = 900
MAX_ANNOUNCEMENTS = 6
MAX_ANNOUNCEMENT_CHARS = 500
MAX_CONTEXT_CHARS = 12000  # hard ceiling on the whole assembled block

# guild_id -> {channel_id -> [pin text, ...]}, plus last-refresh timestamps.
_pins: dict[int, dict[int, list[str]]] = {}
_pins_refreshed_at: dict[int, float] = {}


# ---------------------------------------------------------------------------
# Visibility gate
# ---------------------------------------------------------------------------


def can_view(channel, viewer) -> bool:
    """True if ``viewer`` may see ``channel`` and it isn't an NSFW channel.

    ``viewer`` is a Member (the asker) or a Role (``guild.default_role`` as the
    public fallback). Any error resolving permissions fails closed (excluded).
    """
    try:
        if channel.is_nsfw():
            return False
        return bool(channel.permissions_for(viewer).view_channel)
    except Exception:
        return False


def visible_text_channels(guild, viewer) -> list:
    """Text channels ``viewer`` may see (``viewer=None`` → public @everyone).

    Shared by the context builder and the web surface (which uses it to label
    ``<#id>`` mentions), so both agree on exactly what the asker can see.
    """
    vis = viewer if viewer is not None else getattr(guild, "default_role", None)
    if vis is None:
        return []
    return [ch for ch in getattr(guild, "text_channels", []) if can_view(ch, vis)]


# ---------------------------------------------------------------------------
# Capability summary (what the asker can *do*)
# ---------------------------------------------------------------------------


def _can_see_config(member: discord.Member | None) -> bool:
    """Config is admin-accessible, so only admins/manage-server get the summary."""
    if member is None:
        return False
    p = member.guild_permissions
    return bool(p.administrator or p.manage_guild)


def is_staff(member: discord.Member | None) -> bool:
    """Whether the asker is a mod or admin — anyone with a staff-ish power.

    Deliberately wider than :func:`_can_see_config` (admin/manage-server only):
    this drives *answer quality* (which model handles the ask), not access to
    settings, so a message-moderating mod counts even though they can't see
    config.
    """
    if member is None:
        return False
    p = member.guild_permissions
    return bool(
        p.administrator
        or p.manage_guild
        or p.manage_messages
        or p.moderate_members
        or p.kick_members
        or p.ban_members
    )


# Never surface these — the config KV table holds at least one secret
# (spotify_bot_refresh_token), and future *_token/*_secret keys must stay hidden.
_SECRET_KEY_RE = re.compile(
    r"token|secret|refresh|password|passwd|api[_-]?key|webhook|oauth|credential",
    re.I,
)
_CONFIG_VALUE_MAXLEN = 200
_CONFIG_MAX_LINES = 80  # per feature section
_CONFIG_MAX_CHARS = 9000  # whole settings block


def _fmt_value(guild, key: str, val):
    """Render one setting value readably: ids → names, flags → on/off, else str."""
    if val is None:
        return None
    if isinstance(val, bool):
        return "on" if val else "off"
    low = key.lower()
    ival: int | None = None
    if isinstance(val, int):
        ival = val
    elif isinstance(val, str) and val.strip().lstrip("-").isdigit():
        ival = int(val.strip())
    if ival is not None and ival != 0:
        try:
            if low.endswith(("channel_id", "channel")):
                ch = guild.get_channel(ival)
                return f"#{ch.name}" if ch else f"channel {ival}"
            if low.endswith(("role_id", "role")):
                r = guild.get_role(ival)
                return f"@{r.name}" if r else f"role {ival}"
        except (TypeError, AttributeError):
            pass
    if isinstance(val, str):
        s = val.strip()
        if s == "1":
            return "on"
        if s == "0":
            return "off"
        return s[:_CONFIG_VALUE_MAXLEN] or None
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, (list, tuple, set, frozenset)):
        return f"{len(val)} configured" if val else None
    if isinstance(val, dict):
        return f"{len(val)} configured" if val else None
    return str(val)[:_CONFIG_VALUE_MAXLEN]


def _to_flat_dict(obj) -> dict | None:
    """Coerce a loader's return (dataclass / Row / dict / list) to a flat dict."""
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, sqlite3.Row):
        return dict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, (list, tuple)):
        return {"entries": len(obj)} if obj else None
    d = getattr(obj, "__dict__", None)
    return dict(d) if d else None


def _feature_section(guild, label: str, obj) -> str:
    """One `[Label]` block of `key = value` lines for a feature's settings."""
    d = _to_flat_dict(obj)
    if not d:
        return ""
    lines: list[str] = []
    for key, val in d.items():
        skey = str(key)
        if _SECRET_KEY_RE.search(skey):
            continue
        fv = _fmt_value(guild, skey, val)
        if fv is None:
            continue
        lines.append(f"{skey} = {fv}")
        if len(lines) >= _CONFIG_MAX_LINES:
            break
    return f"[{label}]\n" + "\n".join(lines) if lines else ""


def _mk_getter(module: str, func: str, kind: str) -> Callable:
    """Lazily import a loader and adapt its call shape ('conn' vs 'dbpath')."""
    def _get(conn, guild_id, db_path):
        loader = getattr(importlib.import_module(module), func)
        return loader(db_path, guild_id) if kind == "dbpath" else loader(conn, guild_id)
    return _get


# Feature settings that live in their own tables (not the shared config KV).
# Each getter is (conn, guild_id, db_path) -> dataclass|Row|dict|list|None.
# Failures are isolated per feature, so a bad loader just drops that section.
_FEATURE_LOADERS: list[tuple[str, Callable]] = [
    ("Economy", _mk_getter("bot_modules.services.economy_service", "load_econ_settings", "conn")),
    ("XP", _mk_getter("bot_modules.core.xp_system", "load_xp_settings", "conn")),
    ("QA rewards", _mk_getter("bot_modules.services.qa_service", "load_qa_settings", "conn")),
    ("Voice Master", _mk_getter("bot_modules.services.voice_master_service", "load_voice_master_config", "conn")),
    ("Wellness", _mk_getter("bot_modules.services.wellness_service", "get_wellness_config", "conn")),
    ("Chat Revive", _mk_getter("bot_modules.services.chat_revive_service", "get_guild_config", "conn")),
    ("Starboard", _mk_getter("bot_modules.services.starboard_service", "get_starboard_config", "conn")),
    ("DM permissions", _mk_getter("bot_modules.services.dm_perms_service", "get_dms_config_with_conn", "conn")),
    ("Whisper", _mk_getter("bot_modules.services.whisper_repo", "get_whisper_config", "conn")),
    ("Guess", _mk_getter("bot_modules.services.guess_repo", "get_guess_config", "conn")),
    ("Confessions", _mk_getter("bot_modules.services.confessions_service", "get_config_conn", "conn")),
    ("Grant roles", _mk_getter("bot_modules.core.db_utils", "get_grant_roles", "conn")),
    ("Booster roles", _mk_getter("bot_modules.services.booster_roles", "get_booster_roles", "conn")),
    ("Auto-delete", _mk_getter("bot_modules.services.auto_delete_service", "list_auto_delete_rules_for_guild_with_conn", "conn")),
    ("Auto-react", _mk_getter("bot_modules.services.auto_react_service", "list_auto_react_rules_for_guild_with_conn", "conn")),
    ("Voice transcription", _mk_getter("bot_modules.services.voice_transcription_service", "get_config", "conn")),
    ("Inactivity prune", _mk_getter("bot_modules.services.inactivity_prune_service", "get_prune_rule", "dbpath")),
]


def _slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


_FEATURES_BY_SLUG: dict[str, tuple[str, Callable]] = {
    _slugify(label): (label, getter) for label, getter in _FEATURE_LOADERS
}

# "general" is the shared config KV table (welcome, moderation, spoiler, …).
FEATURE_KEYS: list[str] = ["general", *_FEATURES_BY_SLUG]

can_see_config = _can_see_config  # public alias for surface wiring


def fetch_feature_settings(
    guild, member: discord.Member | None, db_path, feature: str
) -> str:
    """One feature's settings on demand — the handler behind Billy-bot's
    ``get_server_settings`` tool.

    Same formatting, secret filtering, and admin gate as the inline summary,
    but fetched per question instead of dumped into every prompt. Returns
    human/model-readable text in every case (errors included) — the tool
    result goes straight back to the model.
    """
    if not _can_see_config(member):
        return "Not available: only server admins can view server settings."
    slug = _slugify(feature or "")
    try:
        with open_db(db_path) as conn:
            if slug == "general":
                sec = _kv_config_section(conn, guild)
            elif slug in _FEATURES_BY_SLUG:
                label, getter = _FEATURES_BY_SLUG[slug]
                sec = _feature_section(guild, label, getter(conn, guild.id, db_path))
            else:
                return (
                    f"Unknown feature '{feature}'. "
                    f"Available: {', '.join(FEATURE_KEYS)}."
                )
    except Exception:
        log.exception("advisor settings fetch failed: %s / %s", getattr(guild, "id", "?"), slug)
        return "Couldn't read those settings just now — suggest the dashboard panel instead."
    return sec or (
        f"No saved settings for '{slug}' — the feature may be unconfigured. "
        "Point the admin to its dashboard panel."
    )


def _kv_config_section(conn, guild) -> str:
    """The shared config KV table (welcome, moderation, spoiler, …), secret-filtered."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM config WHERE guild_id = ? ORDER BY key",
            (guild.id,),
        ).fetchall()
    except Exception:
        log.exception("advisor KV config read failed for guild %s", getattr(guild, "id", "?"))
        return ""
    lines: list[str] = []
    for row in rows:
        key = row["key"]
        if _SECRET_KEY_RE.search(key):
            continue
        raw = row["value"]
        if raw is None or len(str(raw)) > _CONFIG_VALUE_MAXLEN:
            continue  # skip long blobs (prompts / JSON)
        fv = _fmt_value(guild, key, raw)
        if fv is None:
            continue
        lines.append(f"{key} = {fv}")
        if len(lines) >= _CONFIG_MAX_LINES:
            break
    return "[General]\n" + "\n".join(lines) if lines else ""


def build_config_summary(conn, guild, member: discord.Member | None, db_path=None) -> str:
    """A secret-filtered, name-resolved view of the guild's settings across features.

    Only returned to admins. Combines the shared `config` KV table with each
    feature's own settings loader (economy, wellness, voice master, …). Values
    are shown as raw `field = value` lines so the model reads them directly.
    """
    if not _can_see_config(member):
        return ""

    sections: list[str] = []
    kv = _kv_config_section(conn, guild)
    if kv:
        sections.append(kv)

    total = len(kv)
    for label, getter in _FEATURE_LOADERS:
        if total > _CONFIG_MAX_CHARS:
            break
        try:
            sec = _feature_section(guild, label, getter(conn, guild.id, db_path))
        except Exception:
            log.debug("advisor config loader %s failed", label, exc_info=True)
            continue
        if sec:
            sections.append(sec)
            total += len(sec)

    if not sections:
        return ""
    header = (
        "Server settings — the asker is an admin, so you can see these. Only "
        "what's listed here is visible to you; if a setting or feature isn't "
        "here, say you can't see it and point to its dashboard panel. Values are "
        "raw field names (e.g. welcome_channel_id = #welcome); read them sensibly.\n\n"
    )
    return (header + "\n\n".join(sections))[: _CONFIG_MAX_CHARS + 500]


def capability_summary(member: discord.Member | None) -> str:
    """One line describing the asker's powers, for answer tailoring."""
    if member is None:
        return "The person asking is a regular member (no special permissions)."

    p = member.guild_permissions
    name = getattr(member, "display_name", None) or getattr(member, "name", "a member")
    if p.administrator:
        powers = "is a server administrator (full control)"
    else:
        parts: list[str] = []
        if p.manage_guild:
            parts.append("manage server settings")
        if p.manage_roles:
            parts.append("manage roles")
        if p.manage_channels:
            parts.append("manage channels")
        if p.manage_messages:
            parts.append("moderate messages (pin/delete/purge)")
        if p.moderate_members or p.kick_members or p.ban_members:
            parts.append("take moderator actions on members")
        powers = "can " + ", ".join(parts) if parts else "is a regular member with no moderator or admin powers"

    roles = [r.name for r in getattr(member, "roles", []) if getattr(r, "name", "") not in ("@everyone", "")]
    role_note = f" Roles: {', '.join(roles)}." if roles else ""
    return f"The person asking is {name} and {powers}.{role_note}"


# ---------------------------------------------------------------------------
# Pins snapshot (refreshed in the background)
# ---------------------------------------------------------------------------


def _pin_text(message: discord.Message) -> str | None:
    body = (getattr(message, "content", "") or "").strip().replace("\n", " ")
    if not body:
        embeds = getattr(message, "embeds", None) or []
        if embeds:
            e = embeds[0]
            body = ((getattr(e, "title", "") or "") + " " + (getattr(e, "description", "") or "")).strip()
    body = body.strip()
    return body[:MAX_PIN_CHARS] if body else None


async def refresh_guild_pins(guild: discord.Guild) -> dict[int, list[str]]:
    """Fetch pins for every non-NSFW text channel the bot can read; cache them."""
    result: dict[int, list[str]] = {}
    count = 0
    for ch in getattr(guild, "text_channels", []):
        if count >= MAX_CHANNELS:
            break
        try:
            if ch.is_nsfw():
                continue
            pins = await ch.pins()
        except (discord.Forbidden, discord.HTTPException):
            continue
        except Exception:
            log.debug("pins fetch failed for channel %s", getattr(ch, "id", "?"))
            continue
        texts = [t for m in pins[:MAX_PINS_PER_CHANNEL] if (t := _pin_text(m))]
        if texts:
            result[ch.id] = texts
        count += 1
    _pins[guild.id] = result
    _pins_refreshed_at[guild.id] = time.time()
    return result


async def guild_pins_loop(bot, db_path, *, interval: float = 1800.0) -> None:
    """Background loop: keep each guild's pin snapshot fresh (default 30 min)."""
    import asyncio

    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in list(getattr(bot, "guilds", [])):
            try:
                await refresh_guild_pins(guild)
            except Exception:
                log.exception("pin snapshot failed for guild %s", getattr(guild, "id", "?"))
            await asyncio.sleep(2)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def build_asker_context(
    guild: discord.Guild,
    viewer: discord.Member | None,
    db_path,
    *,
    include_config: bool = True,
) -> str:
    """Assemble the live, visibility-scoped server context block for one asker.

    ``viewer`` is the asking Member when known; when it's ``None`` (e.g. a
    dashboard user we couldn't resolve to a guild member) we fall back to the
    guild's public (@everyone) visibility.

    ``include_config=False`` skips the inline admin settings dump — used when
    the caller gives Billy-bot the ``get_server_settings`` tool instead, so
    settings are fetched on demand rather than paid for on every ask.
    """
    visible = visible_text_channels(guild, viewer)
    visible_ids = {ch.id for ch in visible}

    sections: list[str] = [capability_summary(viewer)]

    # Channels the asker can see (name + <#id> mention + topic).
    topic_lines = [
        f"#{ch.name} (<#{ch.id}>) — {ch.topic.strip()[:MAX_TOPIC_CHARS]}"
        for ch in visible
        if getattr(ch, "topic", None)
    ]
    if topic_lines:
        sections.append("Channels you can see:\n" + "\n".join(topic_lines))

    # Pinned messages, from the snapshot, restricted to visible channels.
    guild_pins = _pins.get(getattr(guild, "id", 0), {})
    pin_blocks: list[str] = []
    for ch in visible:
        texts = guild_pins.get(ch.id)
        if texts:
            pin_blocks.append(f"Pinned in #{ch.name} (<#{ch.id}>):\n- " + "\n- ".join(texts))
    if pin_blocks:
        sections.append("Pinned messages:\n" + "\n\n".join(pin_blocks))

    # Server docs, announcements, and (for admins) core settings come from the DB.
    config_text = ""
    try:
        with open_db(db_path) as conn:
            docs = list_docs(conn, guild.id)
            anns = list_announcements(conn, guild.id)
            if include_config:
                config_text = build_config_summary(conn, guild, viewer, db_path)
    except Exception:
        log.exception("advisor context DB read failed for guild %s", getattr(guild, "id", "?"))
        docs, anns = [], []

    # High-signal for admins — place right after the "who's asking" line.
    if config_text:
        sections.insert(1, config_text)

    doc_lines = []
    for d in docs[:MAX_DOCS]:
        body = (d.get("body_md") or "").strip()[:MAX_DOC_CHARS]
        if body:
            doc_lines.append(f"## {d.get('title') or d.get('doc_key')}\n{body}")
    if doc_lines:
        sections.append("Server docs:\n" + "\n\n".join(doc_lines))

    # Recent *sent* announcements whose target channel the asker can see.
    ann_lines = []
    for row in anns:
        if row["status"] != "sent":
            continue
        ch_id = row["sent_channel_id"] or row["channel_id"]
        if ch_id and ch_id not in visible_ids:
            continue
        title = (row["title"] or "").strip()
        body = (row["body"] or "").strip()[:MAX_ANNOUNCEMENT_CHARS]
        entry = (f"**{title}** — " if title else "") + body
        if entry.strip():
            ann_lines.append(entry.strip())
        if len(ann_lines) >= MAX_ANNOUNCEMENTS:
            break
    if ann_lines:
        sections.append("Recent announcements:\n" + "\n".join(f"- {a}" for a in ann_lines))

    return "\n\n".join(sections)[:MAX_CONTEXT_CHARS]
