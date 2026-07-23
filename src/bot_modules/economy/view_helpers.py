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
