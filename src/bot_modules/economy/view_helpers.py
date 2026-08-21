"""Shared helpers for the economy card/view modules (bounty, pin, auction, …).

These are trivial but were copy-pasted into every view module; centralizing
keeps the currency vocabulary and the "reply without blowing up" behavior in one
place so a change lands everywhere at once.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

log = logging.getLogger("dungeonkeeper.economy")


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


safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ view")




async def edit_review_card(
    card: "discord.Message | None",
    accent,
    settings: "EconSettings",
    row,
    *,
    build_embed,
    log_label: str,
) -> None:
    """Re-render a paid-submission review card in place, best effort.

    ``build_embed`` stays a caller's argument on purpose. Pins and sponsored
    questions render different embeds from different columns, and that copy
    belongs with the product it speaks for — only the edit-and-swallow
    mechanics are shared. Losing the card is not worth raising over: the row
    is already resolved and the member has already been told.
    """
    if card is None:
        return
    try:
        await card.edit(embed=build_embed(accent, settings, row), view=None)
    except discord.HTTPException:
        log.debug("%s: failed to edit card", log_label, exc_info=True)


async def refresh_review_card(
    card: "discord.Message | None",
    ctx,
    accent,
    settings: "EconSettings",
    submission_id: int,
    *,
    read_row,
    build_embed,
    log_label: str,
) -> None:
    """Reload a row that moved underneath its card and re-render it.

    The row can change without the card knowing: a mod resolves it from the
    dashboard, or two mods press buttons at once. Re-reading before the edit
    is what stops a card claiming "approved" after the row went the other
    way. A failed read leaves the card as it is rather than blanking it —
    stale beats wrong-and-confident.
    """
    if card is None:
        return

    def _read():
        with ctx.open_db() as conn:
            return read_row(conn, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:
        log.debug("%s: failed to reload for refresh", log_label, exc_info=True)
        return
    if row is not None:
        await edit_review_card(
            card, accent, settings, row, build_embed=build_embed, log_label=log_label
        )
