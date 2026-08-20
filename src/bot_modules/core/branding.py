"""Shared per-guild embed accent-color resolution.

``resolve_accent_color`` is the one place cogs and the web layer call to
decide what color an embed's accent bar should be. It honors the
guild's branding config:

* ``custom`` mode  → the stored hex color.
* ``avatar`` mode  → a vivid highlight extracted from the guild bot
  avatar (cached by avatar hash), falling back to the bot's role color
  then Discord blurple.

The avatar extraction result is cached process-wide keyed by the avatar
hash, so a new avatar (its hash changes) refreshes automatically with no
explicit invalidation. Custom colors are returned directly without
caching (a trivial DB read + int).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import discord

from bot_modules.core.image_color import dominant_highlight_color
from bot_modules.services.branding_service import (
    ACCENT_MODE_CUSTOM,
    DEFAULT_ACCENT,
    get_branding,
)

log = logging.getLogger(__name__)

# guild_id -> (avatar_key, resolved_color) for avatar-derived accents.
_avatar_cache: dict[int, tuple[str, discord.Color]] = {}


def _fallback_color(me: discord.Member | None) -> discord.Color:
    if me is not None and me.color.value:
        return me.color
    return discord.Color(DEFAULT_ACCENT)


async def resolve_accent_color(db_path: Path, guild: discord.Guild) -> discord.Color:
    """Return the embed accent color for ``guild`` per its branding config."""
    cfg = get_branding(db_path, guild.id)
    if cfg.normalized_mode() == ACCENT_MODE_CUSTOM and cfg.has_custom_color():
        return discord.Color(cfg.accent_hex)

    me = guild.me
    avatar = me.display_avatar if me else None
    if avatar is None:
        return _fallback_color(me)

    cached = _avatar_cache.get(guild.id)
    if cached and cached[0] == avatar.key:
        return cached[1]

    color: discord.Color | None = None
    try:
        data = await avatar.read()
        color = dominant_highlight_color(data)
    except discord.DiscordException:
        color = None
    if color is None:
        color = _fallback_color(me)

    _avatar_cache[guild.id] = (avatar.key, color)
    return color


async def safe_resolve_accent(
    bot: Any,
    guild: discord.Guild | None,
    *,
    default: Any = None,
    log_label: str = "accent",
) -> Any:
    """``resolve_accent_color`` for callers that would rather have a fallback.

    Every game cog wants the same thing: the guild's accent if we can get
    it, and something harmless if we can't — an embed is still worth
    sending when its color bar isn't the branded one. This wraps the real
    resolver so that a missing guild (a DM), a bot with no ``ctx`` yet
    (early startup, or a test double), and a failed DB read all return
    ``default`` instead of raising into an embed builder.

    ``default`` is per-caller because the callers genuinely disagree: most
    pass ``None`` and let discord.py choose, while chicken, musical chairs
    and pressure cooker fall back to their own yellow.
    """
    if guild is None:
        return default
    db_path = getattr(getattr(bot, "ctx", None), "db_path", None)
    if db_path is None:
        return default
    try:
        return await resolve_accent_color(db_path, guild)
    except Exception:
        log.debug(
            "%s: accent resolution failed for guild %s",
            log_label,
            getattr(guild, "id", "?"),
            exc_info=True,
        )
        return default


def invalidate_accent_cache(guild_id: int) -> None:
    """Drop any cached avatar-derived color for a guild.

    Not strictly required (the cache is keyed by avatar hash), but handy
    to force an immediate recompute after a branding change.
    """
    _avatar_cache.pop(guild_id, None)


# A trailing zero-width line. U+200B is a printable char Discord won't strip as
# trailing whitespace, so a value ending in "\n​" renders one extra empty
# line — used to widen the gap before the *next* stacked field's heading.
SECTION_SPACER = "\n​"


def apply_section_spacing(embed: discord.Embed) -> discord.Embed:
    """Give a multi-section embed an even vertical rhythm, in place.

    When an embed stacks several ``inline=False`` fields as sections and a
    field value carries internal blank-line blocks, Discord's field boundary
    is *tighter* than those blanks — so each section heading ends up hugging
    the section above it. Appending :data:`SECTION_SPACER` to every field but
    the last widens the gap before each following heading, so sections read as
    bigger breaks than the items stacked inside them.

    A no-op for embeds with fewer than two fields. Mirrors the convention the
    login digest (``quest_digest``) and weekly leaderboard already apply at
    their string layer; this is the equivalent for builders that assemble a
    ``discord.Embed`` directly. Returns the same embed for convenient chaining.
    """
    for i in range(len(embed.fields) - 1):
        field = embed.fields[i]
        value = field.value or ""
        if not value.endswith(SECTION_SPACER):
            embed.set_field_at(
                i, name=field.name, value=value + SECTION_SPACER, inline=field.inline
            )
    return embed
