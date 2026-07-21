"""Duel/lobby embed styling — accent color + currency vocabulary on wagers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from bot_modules.duels.base_duel import BaseDuel
from bot_modules.duels.base_game import BaseGame, _fmt_coins
from bot_modules.services.economy_service import EconSettings

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems"
)
_ACCENT = discord.Color(0x123456)


def test_fmt_coins_bolded_with_separator_and_plural():
    assert _fmt_coins(_SETTINGS, 1500) == "💎 **1,500** gems"


def test_fmt_coins_singular_at_one():
    assert _fmt_coins(_SETTINGS, 1) == "💎 **1** gem"


def _guild() -> MagicMock:
    guild = MagicMock()
    guild.get_member = lambda uid: SimpleNamespace(display_name=f"U{uid}")
    return guild


def _self(name: str) -> MagicMock:
    holder = MagicMock()
    holder.GAME_DISPLAY_NAME = name
    return holder


def test_lobby_embed_uses_accent_and_currency_vocabulary():
    game = SimpleNamespace(roster=[1, 2], host_id=1, stakes_text=None)
    embed = BaseGame._render_lobby(
        _self("Chicken"), game, _guild(), 2, 8, 10,
        color=_ACCENT, settings=_SETTINGS,
    )
    assert embed.color == _ACCENT
    assert embed.color != discord.Color(0xFFD700)  # not the old COLOR_GOLD
    wager = next(f for f in embed.fields if f.name == "💰 Wager")
    assert "💎 **10** gems to join" in (wager.value or "")
    assert "💎 **20** gems" in (wager.value or "")  # pot = ante × 2 players


def test_lobby_embed_without_settings_still_renders_bare_amount():
    game = SimpleNamespace(roster=[1], host_id=1, stakes_text=None)
    embed = BaseGame._render_lobby(
        _self("Chicken"), game, _guild(), 2, 8, 10, color=_ACCENT, settings=None
    )
    wager = next(f for f in embed.fields if f.name == "💰 Wager")
    assert "**10**" in (wager.value or "")


def test_challenge_embed_uses_accent_and_currency_vocabulary():
    challenger = SimpleNamespace(mention="<@1>")
    target = SimpleNamespace(mention="<@2>")
    embed = BaseDuel._build_challenge_embed(
        _self("Quickdraw"), challenger, target, None, _ACCENT,
        wager=50, settings=_SETTINGS,
    )
    assert embed.color == _ACCENT
    wager = next(f for f in embed.fields if f.name == "💰 Wager")
    assert "💎 **50** gems each" in (wager.value or "")
    assert "💎 **100** gems" in (wager.value or "")  # winner takes 2×
