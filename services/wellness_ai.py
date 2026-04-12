"""Wellness Guardian AI helpers — weekly encouragement generator.

Uses Anthropic claude-haiku-4-5 for warm, 1–2 sentence encouragement notes
attached to weekly reports. Returns a graceful fallback string if no API key
is configured or the request fails.

The system prompt is intentionally tight: warm, concise, no gamification, no
references to specific channels or content. The generated text is shown to
the user inside the weekly report DM, so the tone has to match spec §12.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger("dungeonkeeper.wellness.ai")

WELLNESS_AI_MODEL = "claude-haiku-4-5-20251001"

_ENCOURAGEMENT_SYSTEM = (
    "You are a warm, gentle wellness companion writing one short note for a "
    "Discord user's weekly wellness summary. Tone: like a supportive friend, "
    "not a coach or app. Rules:\n"
    "  - 1–2 sentences total. Never longer.\n"
    "  - Speak directly to the user (\"you\").\n"
    "  - Match the warmth in spec §12: validating, never judgemental.\n"
    "  - No gamification language (\"streak\", \"points\", \"score\", \"win\").\n"
    "  - No references to specific channels, messages, or content.\n"
    "  - No emojis except 💚 (at most one, optional).\n"
    "  - If the user had a tough week, validate the difficulty without minimizing.\n"
    "  - If the user did well, name the specific thing without flattery.\n"
    "Output the note text only — no preamble, no quotation marks, no sign-off."
)

_FALLBACK_GOOD = (
    "You showed up for yourself this week — that matters more than any number on a screen. 💚"
)
_FALLBACK_TOUGH = (
    "Some weeks are harder than others, and that's okay. Tomorrow is another chance to be gentle with yourself. 💚"
)
_FALLBACK_NEUTRAL = (
    "Thanks for being part of this. Whatever this week looked like, you're showing up — and that counts. 💚"
)


def _client() -> "AsyncAnthropic | None":
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        log.warning("anthropic package not installed — wellness AI disabled")
        return None
    return AsyncAnthropic(api_key=api_key)


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
) -> str:
    """Return a 1–2 sentence encouragement note for the user's weekly summary.

    Falls back to a hand-written line if Anthropic isn't configured or fails.
    """
    client = _client()
    if client is None:
        return _fallback_text(streak_days, is_personal_best, compliance_pct)

    facts = (
        f"Current streak: {streak_days} days.\n"
        f"Personal best this week: {'yes' if is_personal_best else 'no'}.\n"
        f"Cap compliance this week: {compliance_pct}%."
    )
    user_content = (
        "Write a single 1–2 sentence encouragement note for this user based on "
        f"these wellness facts:\n\n{facts}"
    )

    try:
        from anthropic.types import TextBlock
        async with client.messages.stream(
            model=WELLNESS_AI_MODEL,
            system=_ENCOURAGEMENT_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=200,
        ) as stream:
            message = await stream.get_final_message()
        parts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        text = "".join(parts).strip().strip('"').strip()
        if not text:
            return _fallback_text(streak_days, is_personal_best, compliance_pct)
        # Hard cap the length to keep weekly reports tight
        if len(text) > 400:
            text = text[:397].rstrip() + "…"
        return text
    except Exception:
        log.exception("wellness_ai: encouragement generation failed; using fallback")
        return _fallback_text(streak_days, is_personal_best, compliance_pct)
