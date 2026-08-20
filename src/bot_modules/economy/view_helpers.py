"""Shared helpers for the economy card/view modules (bounty, pin, auction, …).

These are trivial but were copy-pasted into every view module; centralizing
keeps the currency vocabulary and the "reply without blowing up" behavior in one
place so a change lands everywhere at once.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings


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


