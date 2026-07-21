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

import logging
import time

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


# ---------------------------------------------------------------------------
# Capability summary (what the asker can *do*)
# ---------------------------------------------------------------------------


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
) -> str:
    """Assemble the live, visibility-scoped server context block for one asker.

    ``viewer`` is the asking Member when known; when it's ``None`` (e.g. a
    dashboard user we couldn't resolve to a guild member) we fall back to the
    guild's public (@everyone) visibility.
    """
    vis = viewer if viewer is not None else getattr(guild, "default_role", None)
    visible = [ch for ch in getattr(guild, "text_channels", []) if vis is not None and can_view(ch, vis)]
    visible_ids = {ch.id for ch in visible}

    sections: list[str] = [capability_summary(viewer)]

    # Channels the asker can see (name + topic).
    topic_lines = [
        f"#{ch.name} — {ch.topic.strip()[:MAX_TOPIC_CHARS]}"
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
            pin_blocks.append(f"Pinned in #{ch.name}:\n- " + "\n- ".join(texts))
    if pin_blocks:
        sections.append("Pinned messages:\n" + "\n\n".join(pin_blocks))

    # Server docs + announcements come from dedicated DB tables.
    try:
        with open_db(db_path) as conn:
            docs = list_docs(conn, guild.id)
            anns = list_announcements(conn, guild.id)
    except Exception:
        log.exception("advisor context DB read failed for guild %s", getattr(guild, "id", "?"))
        docs, anns = [], []

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
