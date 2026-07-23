"""Semantic colors on the economy approval cards that have no view test module
of their own — the bounty board card and the Pin of the Day review card.

The style guide's 2026-07-21 ruling names one canonical semantic pair
(``COLOR_GREEN``/``COLOR_RED`` in ``services/embeds.py``); a card resolving to
``discord.Color.green()``/``.red()`` is a second, off-brand shade.
"""
from __future__ import annotations

import discord

from bot_modules.economy.bounty_views import render_bounty_card
from bot_modules.economy.pin_views import render_pin_review_embed
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems"
)
_ACCENT = discord.Color.blurple()


def _bounty(state: str) -> discord.Embed:
    row = {
        "state": state,
        "title": "Draw the mascot",
        "description": "Any medium.",
        "poster_id": 1,
        "winner_id": 2,
        "payout": 900,
        "rake_amount": 100,
    }
    return render_bounty_card(_ACCENT, _SETTINGS, row, pot=1000, contributors=3)


def _pin(state: str) -> discord.Embed:
    return render_pin_review_embed(
        _ACCENT,
        _SETTINGS,
        sponsor_mention="<@1>",
        message="Raid at 8pm.",
        price=300,
        state=state,
        resolver_id=2,
        deny_reason="off-topic",
    )


def test_bounty_card_uses_the_canonical_semantic_pair():
    assert _bounty("awarded").color == discord.Color(COLOR_GREEN)
    assert _bounty("cancelled").color == discord.Color(COLOR_RED)
    assert _bounty("expired").color == discord.Color(COLOR_RED)


def test_bounty_card_open_state_follows_the_accent():
    assert _bounty("open").color == _ACCENT


def test_pin_review_card_uses_the_canonical_semantic_pair():
    assert _pin("live").color == discord.Color(COLOR_GREEN)
    assert _pin("denied").color == discord.Color(COLOR_RED)
    assert _pin("superseded").color == discord.Color(COLOR_RED)


def test_pin_review_card_pending_follows_the_accent():
    assert _pin("pending").color == _ACCENT
