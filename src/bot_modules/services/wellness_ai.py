"""Wellness Guardian encouragement — always returns a fallback note."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_FALLBACK_GOOD = "You showed up for yourself this week — that matters more than any number on a screen. 💚"
_FALLBACK_TOUGH = "Some weeks are harder than others, and that's okay. Tomorrow is another chance to be gentle with yourself. 💚"
_FALLBACK_NEUTRAL = "Thanks for being part of this. Whatever this week looked like, you're showing up — and that counts. 💚"


def _fallback_text(streak_days: int, is_personal_best: bool, compliance_pct: int) -> str:
    if is_personal_best or compliance_pct >= 80:
        return _FALLBACK_GOOD
    if compliance_pct < 40 or streak_days <= 1:
        return _FALLBACK_TOUGH
    return _FALLBACK_NEUTRAL


async def generate_weekly_encouragement(
    *,
    streak_days: int,
    is_personal_best: bool,
    compliance_pct: int,
    db_path: Path | None = None,
) -> str:
    return _fallback_text(streak_days, is_personal_best, compliance_pct)
