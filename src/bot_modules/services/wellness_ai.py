"""Wellness Guardian encouragement — uses Ollama when available, falls back to static text."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("dungeonkeeper.wellness_ai")

_FALLBACK_GOOD = "You showed up for yourself this week — that matters more than any number on a screen. 💚"
_FALLBACK_TOUGH = "Some weeks are harder than others, and that's okay. Tomorrow is another chance to be gentle with yourself. 💚"
_FALLBACK_NEUTRAL = "Thanks for being part of this. Whatever this week looked like, you're showing up — and that counts. 💚"

_ENCOURAGEMENT_SYSTEM = (
    "You are a warm, compassionate wellness companion for a private adult Discord community. "
    "A member has just completed their weekly wellness check-in. Write a brief, genuine "
    "encouragement note (2–4 sentences) based on their stats. Be warm but not saccharine. "
    "Do not repeat their numbers back to them verbatim. End with a heart emoji. "
    "Respond with only the note — no greeting, no sign-off."
)


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
    from bot_modules.services import ollama_client

    if not ollama_client.is_available():
        return _fallback_text(streak_days, is_personal_best, compliance_pct)

    from bot_modules.services.ai_config import get_prompt_from_path, get_wellness_model_from_path

    model = get_wellness_model_from_path(db_path) if db_path else ollama_client.default_model()
    system = (
        get_prompt_from_path(db_path, "ai_prompt_wellness_encouragement")
        if db_path
        else _ENCOURAGEMENT_SYSTEM
    )

    streak_label = f"{streak_days} day{'s' if streak_days != 1 else ''}"
    pb_note = " (a new personal best!)" if is_personal_best else ""
    user_content = (
        f"Member stats: {streak_label} streak{pb_note}, "
        f"{compliance_pct}% compliance this week."
    )

    try:
        result = await ollama_client.chat(
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=256,
        )
        return result or _fallback_text(streak_days, is_personal_best, compliance_pct)
    except Exception as exc:
        log.warning("Ollama wellness generation failed: %s — using fallback.", exc)
        return _fallback_text(streak_days, is_personal_best, compliance_pct)
