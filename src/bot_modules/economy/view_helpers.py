"""Shared helpers for the economy card/view modules (bounty, pin, auction, …).

These are trivial but were copy-pasted into every view module; centralizing
keeps the currency vocabulary and the "reply without blowing up" behavior in one
place so a change lands everywhere at once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

log = logging.getLogger("dungeonkeeper.economy")


# Discord rejects an embed field over 1024 chars — and rejects the whole
# embed with it, not just the offending field.
EMBED_FIELD_LIMIT = 1024


def fit_lines(lines: list[str], limit: int = EMBED_FIELD_LIMIT) -> str:
    """Join as many leading lines as fit an embed field.

    Variable-length rows (wallet memos, quest titles) can overrun the field
    cap and make Discord reject the entire embed. Dropping the overflow keeps
    the leading rows visible rather than 400-ing the whole render.
    """
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(line)
        used += cost
    return "\n".join(out)


def unit(settings: EconSettings, amount: int) -> str:
    """Currency name matching ``amount``'s grammatical number.

    Note the deliberate difference from ``coins`` below: this returns the
    configured plural verbatim, including an empty one. Callers that render
    the bare unit (the wallet header, quest rewards) have always shown
    whatever the guild configured; ``coins`` substitutes a literal fallback.
    Don't "unify" the two without deciding which behaviour a guild with an
    empty ``currency_plural`` should get.
    """
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def coins(settings: EconSettings, amount: int) -> str:
    """``🪙 **250** coins`` — the currency vocabulary every economy card uses."""
    unit = (
        settings.currency_name
        if abs(amount) == 1
        else (settings.currency_plural or "coins")
    )
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


async def safe_ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Send an ephemeral reply, honoring whether the interaction was deferred.

    Swallows the HTTP error (a dead/expired interaction is not worth raising
    into a button handler) and logs it at debug.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ view: failed to send ephemeral", exc_info=True)
