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

from pathlib import Path

import discord

from bot_modules.core.image_color import dominant_highlight_color
from bot_modules.services.branding_service import (
    ACCENT_MODE_CUSTOM,
    DEFAULT_ACCENT,
    get_branding,
)

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


def invalidate_accent_cache(guild_id: int) -> None:
    """Drop any cached avatar-derived color for a guild.

    Not strictly required (the cache is keyed by avatar hash), but handy
    to force an immediate recompute after a branding change.
    """
    _avatar_cache.pop(guild_id, None)
