"""Duel/lobby embed styling — accent color + currency vocabulary on wagers."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.cogs.quickdraw.cog import QuickdrawDuel
from bot_modules.duels.base_duel import BaseDuel
from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS
from bot_modules.duels.filters import resolve_stakes_text
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
    pot = next(f for f in embed.fields if f.name == "💰 Pot")
    assert "💎 **20** gems" in (pot.value or "")  # pot = ante × 2 players


def test_lobby_embed_pot_field_does_not_repeat_the_ante():
    """The ante is already a line in the stakes field since the stakes started
    stacking, so printing it here too showed the same number twice on every
    join/leave re-render. This field's job is the pot, which the stakes string
    can't carry because it grows."""
    game = SimpleNamespace(
        roster=[1, 2], host_id=1,
        stakes_text="💰 💎 **10** gems to join — winner takes the pot.",
    )
    embed = BaseGame._render_lobby(
        _self("Chicken"), game, _guild(), 2, 8, 10,
        color=_ACCENT, settings=_SETTINGS,
    )
    stakes = next(f for f in embed.fields if f.name == "📋 Stakes")
    pot = next(f for f in embed.fields if f.name == "💰 Pot")
    assert "💎 **10** gems to join" in (stakes.value or "")
    assert "to join" not in (pot.value or "")
    assert "**10**" not in (pot.value or "")


def test_lobby_embed_without_settings_still_renders_bare_amount():
    game = SimpleNamespace(roster=[1], host_id=1, stakes_text=None)
    embed = BaseGame._render_lobby(
        _self("Chicken"), game, _guild(), 2, 8, 10, color=_ACCENT, settings=None
    )
    pot = next(f for f in embed.fields if f.name == "💰 Pot")
    assert "**10**" in (pot.value or "")  # one player in the lobby → pot == ante


def test_challenge_embed_lists_every_stake_in_one_field():
    """The wager used to sit in its own field while the in-game and result
    embeds showed only the custom stakes, so a two-stake game read as
    one-stake until settlement. The persisted text now carries all of them,
    already in the guild's currency vocabulary, and the card renders it."""
    challenger = SimpleNamespace(mention="<@1>")
    target = SimpleNamespace(mention="<@2>")
    stakes = resolve_stakes_text(
        "loser sings", 50, nick_stake=True,
        wager_line="💰 💎 **50** gems each — winner takes 💎 **100** gems.",
    )
    embed = BaseDuel._build_challenge_embed(
        _self("Quickdraw"), challenger, target, stakes, _ACCENT, wager=50,
    )
    assert embed.color == _ACCENT
    field = next(f for f in embed.fields if f.name == "📋 Stakes")
    value = field.value or ""
    assert "loser sings" in value
    assert "💎 **50** gems each" in value
    assert "💎 **100** gems" in value  # winner takes 2×
    assert "nickname" in value.lower()
    assert "Nothing is charged" in value


def test_challenge_embed_counts_down_instead_of_stating_a_number():
    """A static "60 seconds to respond" footer never moved, and a footer can't
    carry a Discord timestamp anyway — so the deadline is a field, and the
    client ticks it down."""
    embed = BaseDuel._build_challenge_embed(
        _self("Quickdraw"), SimpleNamespace(mention="<@1>"),
        SimpleNamespace(mention="<@2>"), None, _ACCENT,
    )
    field = next(f for f in embed.fields if "Expires" in (f.name or ""))
    value = field.value or ""
    assert value.startswith("<t:") and value.endswith(":R>")
    deadline = int(value[3:-3])
    assert deadline - int(time.time()) == pytest.approx(
        CHALLENGE_RESPONSE_SECONDS, abs=2
    )
    assert embed.footer.text is None


def test_challenge_embed_plain_duel_keeps_the_nickname_fallback():
    challenger = SimpleNamespace(mention="<@1>")
    target = SimpleNamespace(mention="<@2>")
    embed = BaseDuel._build_challenge_embed(
        _self("Quickdraw"), challenger, target, None, _ACCENT,
    )
    field = next(f for f in embed.fields if f.name == "📋 Stakes")
    assert "nickname" in (field.value or "").lower()
    assert "Nothing is charged" not in (field.value or "")


# ── the owner's sentence is honest about not having been applied ─────────────


def test_result_embed_self_apply_does_not_claim_the_rename_happened():
    """Discord blocks renaming the guild owner, so the sentence stands but the
    bot never applied it. The embed used to say "is now known as", which is
    simply untrue — the owner had to do it by hand (game night 2026-08-21)."""
    game = SimpleNamespace(
        winner_id=1, loser_id=2, stakes_text=None, nick_stake=True, roster=[1, 2],
        fired_at=None, resolved_at=None, loser_fired_at=None,
    )
    embed = QuickdrawDuel.render_result_state(
        _self("Quickdraw"), game, _guild(), self_apply_nick="Wet Willy",
        original_name="Billy",
    )
    field = next(f for f in embed.fields if "Nickname" in (f.name or ""))
    assert "Wet Willy" in (field.value or "")
    assert "themselves" in (field.value or "")
    assert "is now known as" not in (field.value or "")


def test_result_embed_normal_rename_still_says_it_was_applied():
    game = SimpleNamespace(
        winner_id=1, loser_id=2, stakes_text=None, nick_stake=True, roster=[1, 2],
        fired_at=None, resolved_at=None, loser_fired_at=None,
    )
    embed = QuickdrawDuel.render_result_state(
        _self("Quickdraw"), game, _guild(), imposed_nick="Wet Willy",
        original_name="Billy",
    )
    field = next(f for f in embed.fields if "Nickname" in (f.name or ""))
    assert "is now known as" in (field.value or "")
