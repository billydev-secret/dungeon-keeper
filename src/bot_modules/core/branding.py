"""Shared per-guild embed accent-colour resolution.

``resolve_accent_color`` is the one place cogs and the web layer call to
decide what colour an embed's accent bar should be. It honours the
guild's branding config:

* ``custom`` mode  → the stored hex colour.
* ``avatar`` mode  → a vivid highlight extracted from the guild bot
  avatar (cached by avatar hash), falling back to the bot's role colour
  then Discord blurple.

The avatar extraction result is cached process-wide keyed by the avatar
hash, so a new avatar (its hash changes) refreshes automatically with no
explicit invalidation. Custom colours are returned directly without
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

# guild_id -> (avatar_key, resolved_colour) for avatar-derived accents.
_avatar_cache: dict[int, tuple[str, discord.Colour]] = {}


def _fallback_colour(me: discord.Member | None) -> discord.Colour:
    if me is not None and me.colour.value:
        return me.colour
    return discord.Colour(DEFAULT_ACCENT)


async def resolve_accent_color(db_path: Path, guild: discord.Guild) -> discord.Colour:
    """Return the embed accent colour for ``guild`` per its branding config."""
    cfg = get_branding(db_path, guild.id)
    if cfg.normalized_mode() == ACCENT_MODE_CUSTOM and cfg.has_custom_colour():
        return discord.Colour(cfg.accent_hex)

    me = guild.me
    avatar = me.display_avatar if me else None
    if avatar is None:
        return _fallback_colour(me)

    cached = _avatar_cache.get(guild.id)
    if cached and cached[0] == avatar.key:
        return cached[1]

    colour: discord.Colour | None = None
    try:
        data = await avatar.read()
        colour = dominant_highlight_color(data)
    except discord.DiscordException:
        colour = None
    if colour is None:
        colour = _fallback_colour(me)

    _avatar_cache[guild.id] = (avatar.key, colour)
    return colour


def invalidate_accent_cache(guild_id: int) -> None:
    """Drop any cached avatar-derived colour for a guild.

    Not strictly required (the cache is keyed by avatar hash), but handy
    to force an immediate recompute after a branding change.
    """
    _avatar_cache.pop(guild_id, None)
