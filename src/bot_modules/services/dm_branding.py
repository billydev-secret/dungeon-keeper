"""Per-guild branding for direct messages.

A DM has no guild context of its own. Discord gives the bot exactly one
username and one avatar globally, so a member who shares two servers with
us sees the identical sender either way — that ceiling is not something
code can move. What *can* carry the guild is the message body: the embed
accent, and an attribution line naming the server (with its icon).

Before this module every DM site solved that alone, and inconsistently:
``dm_perms`` named the guild in eight places but never passed an accent,
while the wellness DMs — a personal boundary tool, where "which server is
nudging me?" is the whole question — named no guild at all. Three
near-identical ``_try_dm`` helpers had accumulated alongside.

Two entry points, deliberately split:

* :func:`brand_dm_embed` is pure and synchronous. It stamps an accent and
  an attribution onto an embed and returns it. Callers that already own a
  delivery policy — ``economy_service.notify_member`` has mute prefs, an
  opt-in role gate, and a bank-channel fallback — brand with this and keep
  their own send path intact.
* :func:`send_branded_dm` is the common case: resolve the accent from the
  guild's branding config, brand, send, swallow a closed DM.

Attribution defaults to the footer, not the author slot. Several DM embeds
already use the author line for something more informative than the server
name (``dm_perms`` puts the requesting member there), so claiming it by
default would cost more than it adds. Pass ``placement=ATTRIBUTION_AUTHOR``
where the guild genuinely is the most useful thing in the header.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal, Optional

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.embeds import DM_PRIMARY

log = logging.getLogger("dungeonkeeper.dm_branding")

ATTRIBUTION_FOOTER = "footer"
ATTRIBUTION_AUTHOR = "author"
ATTRIBUTION_NONE = "none"

Placement = Literal["footer", "author", "none"]

# Joins guild attribution to a footer the builder already set, matching the
# separator used elsewhere in the bot's embed footers.
_FOOTER_SEP = " • "


def brand_dm_embed(
    embed: discord.Embed,
    *,
    guild_name: Optional[str] = None,
    guild_icon_url: Optional[str] = None,
    color: Optional[discord.Color] = None,
    placement: Placement = ATTRIBUTION_FOOTER,
    keep_color: bool = False,
) -> discord.Embed:
    """Stamp ``embed`` with the guild accent and an attribution line.

    Pure: mutates and returns the embed passed in, performs no IO. ``color``
    falls back to :data:`DM_PRIMARY` so an unbranded guild keeps the look DM
    embeds have today rather than going Discord-default grey.

    ``keep_color`` leaves an already-set color alone and applies attribution
    only. Use it where the color carries meaning rather than branding — a
    release-from-hold notice is green because it is *good news*, and
    CLAUDE.md keeps semantic colors out of the accent's reach.

    With ``guild_name`` unset (the bot was kicked, or the caller has no
    guild in hand) the accent is still applied and attribution is skipped —
    a DM with no server name beats no DM at all.
    """
    if not (keep_color and embed.color is not None):
        embed.color = color if color is not None else discord.Color(DM_PRIMARY)

    if not guild_name or placement == ATTRIBUTION_NONE:
        return embed

    if placement == ATTRIBUTION_AUTHOR:
        embed.set_author(name=guild_name, icon_url=guild_icon_url)
        return embed

    # Footer: preserve whatever the builder already put there. Its text is
    # feature-specific ("DM relationships are logged…") and outranks ours,
    # so the server name trails it rather than replacing it.
    existing = (embed.footer.text or "").strip() if embed.footer else ""
    text = f"{existing}{_FOOTER_SEP}{guild_name}" if existing else guild_name
    embed.set_footer(text=text, icon_url=guild_icon_url)
    return embed


def guild_display_name(guild: Optional[discord.Guild]) -> Optional[str]:
    """The guild's name, or None when it has none to give.

    Same tolerance as :func:`guild_icon_url` — attribution is a nicety and
    must never be the reason a DM fails to send.
    """
    return getattr(guild, "name", None) if guild is not None else None


def guild_icon_url(guild: Optional[discord.Guild]) -> Optional[str]:
    """The guild's icon URL, or None for a guild with no icon set.

    Tolerates a guild object that does not carry an ``icon`` at all: the
    attribution text is the part that matters, and a missing icon must not
    be the reason a DM fails to send.
    """
    icon = getattr(guild, "icon", None) if guild is not None else None
    return icon.url if icon is not None else None


async def resolve_dm_accent(
    db_path: Optional[Path], guild: Optional[discord.Guild]
) -> discord.Color:
    """Accent for a DM sent on ``guild``'s behalf, or the DM default.

    Never raises. Resolving an accent can touch the database and, in avatar
    mode, fetch the bot avatar over HTTP — and this now sits on the path of
    DMs that matter a great deal more than their color does (a wellness
    friction notice is withheld unless its DM lands; whisper rolls back a
    stored row). A branding failure degrades to the default rather than
    costing the member the message.
    """
    if guild is None or db_path is None:
        return discord.Color(DM_PRIMARY)
    try:
        return await resolve_accent_color(db_path, guild)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "dm_branding: accent lookup failed for guild %s — using the DM default.",
            getattr(guild, "id", "?"),
            exc_info=True,
        )
        return discord.Color(DM_PRIMARY)


async def send_branded_dm(
    user: discord.abc.Messageable,
    *,
    db_path: Optional[Path],
    guild: Optional[discord.Guild],
    embed: Optional[discord.Embed] = None,
    content: Optional[str] = None,
    placement: Placement = ATTRIBUTION_FOOTER,
    keep_color: bool = False,
    **send_kwargs: Any,
) -> Optional[discord.Message]:
    """Brand ``embed`` for ``guild`` and DM it to ``user``.

    Returns the sent message, or None when the DM could not be delivered —
    a closed DM is an ordinary outcome here, not an error, so callers that
    only care whether it landed can test the result for None. Callers that
    must roll back on failure (whisper deletes its row) get the same signal.

    ``content`` is passed through untouched; branding only ever applies to
    the embed. Extra keyword arguments (``view``, ``allowed_mentions``,
    ``file``) reach ``user.send`` unchanged.
    """
    if embed is not None:
        accent = await resolve_dm_accent(db_path, guild)
        brand_dm_embed(
            embed,
            guild_name=guild_display_name(guild),
            guild_icon_url=guild_icon_url(guild),
            color=accent,
            placement=placement,
            keep_color=keep_color,
        )

    kwargs: dict[str, Any] = dict(send_kwargs)
    if content:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed

    try:
        return await user.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return None
