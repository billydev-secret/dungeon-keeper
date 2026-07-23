"""Tests for the sponsored-question card embed (economy/sponsor_views.py).

Scope is the money vocabulary on the card — the Paid/Refund fields must read
like every other economy surface (emoji, bold, thousands separator, unit that
goes singular at 1), not the old bare ``500 coins``.
"""
from __future__ import annotations

import discord

from bot_modules.economy.sponsor_views import (
    _reward_text,
    render_sponsor_card_embed,
)
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems"
)


def _card(state: str) -> discord.Embed:
    return render_sponsor_card_embed(
        discord.Color.blurple(),
        _SETTINGS,
        sponsor_mention="<@1>",
        question="What's your favourite colour?",
        price=1500,
        state=state,
        resolver_id=2,
        deny_reason="off-topic",
    )


def test_reward_text_matches_the_shared_vocabulary():
    assert _reward_text(_SETTINGS, 1500) == "💎 **1,500** gems"


def test_reward_text_goes_singular_at_one():
    assert _reward_text(_SETTINGS, 1) == "💎 **1** gem"


def test_paid_field_uses_currency_vocabulary_not_bare_number():
    fields = {f.name: (f.value or "") for f in _card("pending").fields}
    assert fields["💰 Paid"] == "💎 **1,500** gems"
    # The old bare rendering must be gone.
    assert "1500 gems" not in fields["💰 Paid"]


def test_refund_field_on_denial_is_formatted_too():
    fields = {f.name: (f.value or "") for f in _card("denied").fields}
    assert fields["↩️ Refund"] == "💎 **1,500** gems returned"


def test_card_uses_the_canonical_semantic_pair():
    """Approved/posted are COLOR_GREEN, declined COLOR_RED — no ad-hoc shades."""
    assert _card("approved").color == discord.Color(COLOR_GREEN)
    assert _card("posted").color == discord.Color(COLOR_GREEN)
    assert _card("denied").color == discord.Color(COLOR_RED)
    assert _card("expired").color == discord.Color(COLOR_RED)
