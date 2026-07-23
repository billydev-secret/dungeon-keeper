"""Casino result-card colors — the win/loss pair must stay the sanctioned
semantic set. A big win is still a win: it gets ``COLOR_GREEN`` like any other,
with the celebration carried by the copy, not by a third color tier.
"""
from __future__ import annotations

import discord

from bot_modules.cogs.casino.embeds import build_slots_embed
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

_ECON = EconSettings(currency_emoji="💎", currency_name="gem", currency_plural="gems")
_REELS = ("🍯", "🍯", "🍯")


def _slots(stake: int, payout: int, *, jackpot: int = 0) -> discord.Embed:
    return build_slots_embed(
        _ECON, 42, _REELS, stake, payout, "Three of a kind!", jackpot_won=jackpot
    )


def test_small_win_is_green():
    assert _slots(10, 20).color == discord.Color(COLOR_GREEN)


def test_big_win_is_the_same_green_not_a_third_tier():
    assert _slots(10, 500).color == discord.Color(COLOR_GREEN)


def test_jackpot_win_is_green_but_keeps_its_copy():
    embed = _slots(10, 500, jackpot=5000)
    assert embed.color == discord.Color(COLOR_GREEN)
    assert embed.title is not None and "HONEYPOT" in embed.title


def test_loss_is_red():
    assert _slots(10, 0).color == discord.Color(COLOR_RED)
