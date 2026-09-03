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
from typing import Any, TypeVar, overload

import discord

from bot_modules.core.image_color import dominant_highlight_color
from bot_modules.services.branding_service import (
    ACCENT_MODE_CUSTOM,
    DEFAULT_ACCENT,
    get_branding,
)

log = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The end of ``resolve_accent_color``'s own fallback chain — what a guild
#: gets when it has no custom color and the bot has no colored role either.
#: Callers that need a non-optional ``discord.Color`` pass this as
#: ``safe_resolve_accent(..., default=DEFAULT_ACCENT_COLOR)`` so a branding
#: failure still yields a color rather than dropping the accent bar. Note it
#: is the *last* link, not "whatever an unbranded guild shows": where the bot
#: has a colored top role, ``_fallback_color`` returns that instead, so a
#: failure here can render a different color than a healthy lookup would.
DEFAULT_ACCENT_COLOR = discord.Color(DEFAULT_ACCENT)

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


def _db_path_from(source: Any) -> Any:
    """Find the branding DB path on whatever the caller happened to hold.

    Call sites reach this helper from every layer of the bot and none of
    them agree on what's in scope: a cog has ``self.bot``, a view has
    ``self.ctx``, a service is handed a bare ``db_path``, and a dashboard
    route has the app context. Rather than make 121 call sites each dig out
    the same attribute — and re-introduce the ``bot.ctx`` AttributeError
    that this helper exists to absorb — accept all three and look.
    """
    if isinstance(source, (str, Path)):
        return source
    ctx = getattr(source, "ctx", None)
    if ctx is not None:
        db_path = getattr(ctx, "db_path", None)
        if db_path is not None:
            return db_path
    return getattr(source, "db_path", None)


@overload
async def safe_resolve_accent(
    source: Any, guild: discord.Guild | None, *, log_label: str = ...
) -> discord.Color | None: ...


@overload
async def safe_resolve_accent(
    source: Any, guild: discord.Guild | None, *, default: _T, log_label: str = ...
) -> discord.Color | _T: ...


async def safe_resolve_accent(
    source: Any,
    guild: discord.Guild | None,
    *,
    default: Any = None,
    log_label: str = "accent",
) -> Any:
    """``resolve_accent_color`` for callers that would rather have a fallback.

    Every caller wants the same thing: the guild's accent if we can get it,
    and something harmless if we can't — an embed is still worth sending
    when its color bar isn't the branded one, and a dashboard page is worth
    rendering. This wraps the real resolver so that a missing guild (a DM),
    a bot with no ``ctx`` yet (early startup, or a test double), and a
    failed DB read all return ``default`` instead of raising into an embed
    builder, a background loop, or an HTTP handler.

    ``source`` is whatever holds the DB path: a Bot, an AppContext, or the
    path itself. See ``_db_path_from``.

    ``default`` is per-caller because the callers genuinely disagree: most
    pass ``None`` and let discord.py choose, while chicken, musical chairs
    and pressure cooker fall back to their own yellow. The overloads carry
    that through the return type, so a caller storing the result in a
    ``dict[int, discord.Color]`` still gets checked.
    """
    if guild is None:
        return default
    db_path = _db_path_from(source)
    if db_path is None:
        if source is not None:
            # An explicit None means "I know I have no context" and stays
            # quiet. A real object that yields no db_path is either a bot
            # whose ctx isn't attached yet or — far more likely — the wrong
            # object: ``safe_resolve_accent(self, ...)`` from a cog that
            # keeps its context on ``self.bot``. Before this helper existed
            # that typo raised AttributeError; silence would make it a
            # permanently unbranded embed that nothing ever reports.
            log.warning(
                "%s: no db_path on %s — accent not resolved for guild %s",
                log_label,
                type(source).__name__,
                getattr(guild, "id", "?"),
            )
        return default
    try:
        return await resolve_accent_color(db_path, guild)
    except Exception:
        # Warning, not debug: the root logger runs at INFO, so a debug line
        # here would be invisible in production — and a branding table that
        # has started raising strips the accent from every embed the bot
        # sends. The guard returns above stay silent because a DM or a
        # ctx-less bot is ordinary; an exception here is not.
        log.warning(
            "%s: accent resolution failed for guild %s",
            log_label,
            getattr(guild, "id", "?"),
            exc_info=True,
        )
        return default


async def prime_accent_cache(
    cache: dict, key: Any, source: Any, guild: discord.Guild | None, *, log_label: str
) -> None:
    """Resolve ``guild``'s accent once and remember it under ``key``.

    For games that render the same embed repeatedly and don't want a branding
    read per edit. Already-cached keys short-circuit, and a failed resolve
    leaves the key *unset* rather than caching a fallback — so the render's own
    default applies now and a later prime can still succeed, instead of the
    first hiccup pinning a wrong colour for the life of the game.
    """
    if key in cache:
        return
    accent = await safe_resolve_accent(source, guild, log_label=log_label)
    if accent is not None:
        cache[key] = accent


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

#: Discord's hard cap on a single embed field's value. A send that breaches it
#: is rejected whole, so the spacer never pushes a field over it.
FIELD_VALUE_LIMIT = 1024


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

    **``inline=True`` fields are skipped.** The spacer exists to stop a section
    heading hugging the value above it, and an inline field has no heading
    below it — it sits *beside* its neighbours, and Discord starts a fresh row
    for whatever follows the group. Spacing one only makes its box taller, and
    on a three-across row that is dead height on every card. This is what let
    the helper be applied to every builder rather than only the ones whose
    fields all stack (ruling 2026-09-03).

    A field already at Discord's 1024-char value cap is **left alone**. Plenty
    of builders fill a field right up to that line (``fit_lines``, a raw
    ``value[:1024]`` slice), and two more characters there would 400 the whole
    embed — losing the card entirely to buy two pixels of air.
    """
    for i in range(len(embed.fields) - 1):
        field = embed.fields[i]
        if field.inline:
            continue
        value = field.value or ""
        if len(value) + len(SECTION_SPACER) > FIELD_VALUE_LIMIT:
            continue
        if not value.endswith(SECTION_SPACER):
            embed.set_field_at(
                i, name=field.name, value=value + SECTION_SPACER, inline=field.inline
            )
    return embed
