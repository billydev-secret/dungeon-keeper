"""Member-facing copy on the six games names the dial, and refuses in the
house shape (duels-party-125 / duels-party-128).

"24 hours" and "5 minutes" were typed into every result card, DM, lobby line
and slash description while ``sentence_hours`` is a dashboard dial and the
sweeps have their own constants: an admin who set Nickname Lasts to 48 got
cards promising 24. Every duration now comes through ``BaseGame``'s copy
helpers, and a static sweep here keeps the literals from creeping back.

Refusals go through one ``_refuse`` helper that prefixes ``❌``; the sentence
refusal stopped claiming the loser "can't play again" (only the nickname
stake is blocked); the lobby cooldown refusal names the time left.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.duels import db as duels_db
from bot_modules.duels.base_duel import BaseDuel
from bot_modules.duels.base_game import BaseGame
from bot_modules.duels.filters import (
    NICK_STAKES_LINE,
    custom_stakes_from,
    nick_stakes_line,
    resolve_stakes_text,
)
from tests.fakes import fake_interaction
from tests.test_duel_rename_original_name import (
    GAMES,
    NEW_NICK,
    OLD_NAME,
    _guild_after_rename,
    _load,
)

_ROOT = Path(__file__).resolve().parents[1] / "src" / "bot_modules"


def _bare() -> BaseGame:
    cog = BaseGame.__new__(BaseGame)
    cog.GAME_DISPLAY_NAME = "Test"
    cog.GAME_KEY = "test"
    return cog


# ── durations ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (300, "5 minutes"),
        (60, "1 minute"),
        (90, "90 seconds"),
        (1800, "30 minutes"),
        (3600, "1 hour"),
        (7200, "2 hours"),
        (172800, "48 hours"),
    ],
)
def test_span_reads_naturally(seconds, expected):
    assert BaseGame._span(seconds) == expected


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        pytest.param(None, "24 hours", id="no-dial-in-hand-uses-the-default"),
        pytest.param(48, "48 hours", id="the-dial"),
        pytest.param(1, "1 hour", id="singular"),
    ],
)
def test_hours_span(hours, expected):
    assert BaseGame._hours_span(hours) == expected
    assert duels_db.default_sentence_hours() == 24


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5400, "1h 30m"), (59, "1m"), (3600, "1h 0m"), (600, "10m")],
)
def test_remaining_rounds_up_to_the_minute(seconds, expected):
    assert BaseGame._remaining(seconds) == expected


# ── every card reads the dial ─────────────────────────────────────────────────


@pytest.mark.parametrize("module, cls, make_game", GAMES, ids=[g[0] for g in GAMES])
def test_result_card_names_the_configured_sentence(module, cls, make_game):
    cog = _load(module, cls)
    embed = cog.render_result_state(
        make_game(), _guild_after_rename(),
        imposed_nick=NEW_NICK, original_name=OLD_NAME, sentence_hours=48,
    )
    field = next(f for f in embed.fields if "Nickname Applied" in (f.name or ""))
    assert field.value == f"**{OLD_NAME}** is now known as **{NEW_NICK}** for 48 hours."


@pytest.mark.parametrize("module, cls, make_game", GAMES, ids=[g[0] for g in GAMES])
def test_awaiting_card_names_the_window_and_the_sentence(module, cls, make_game):
    cog = _load(module, cls)
    embed = cog.render_result_state(make_game(), _guild_after_rename(), sentence_hours=48)
    field = next(f for f in embed.fields if "Awaiting Nickname" in (f.name or ""))
    assert "within 30 minutes" in (field.value or "")
    assert "lasts 48 hours" in (field.value or "")
    assert "24" not in (field.value or "") and "5 minutes" not in (field.value or "")


def test_lobby_card_fallback_names_the_configured_sentence():
    cog = _bare()
    guild = MagicMock()
    guild.get_member = lambda uid: SimpleNamespace(display_name=f"U{uid}")
    game = SimpleNamespace(roster=[1], host_id=1, stakes_text=None)
    embed = cog._render_lobby(
        game, guild, 2, 8, 0, color=discord.Color.blurple(), sentence_hours=48,
    )
    stakes = next(f for f in embed.fields if f.name == "📋 Stakes")
    assert "for 48 hours" in (stakes.value or "")


def test_challenge_card_fallback_names_the_configured_sentence():
    cog = BaseDuel.__new__(BaseDuel)
    cog.GAME_DISPLAY_NAME = "Quickdraw"
    embed = cog._build_challenge_embed(
        SimpleNamespace(mention="<@1>"), SimpleNamespace(mention="<@2>"), None,
        discord.Color.blurple(), sentence_hours=48,
    )
    stakes = next(f for f in embed.fields if f.name == "📋 Stakes")
    # (apply_section_spacing pads every field but the last with a blank line)
    assert (stakes.value or "").startswith("Loser surrenders their nickname for 48 hours.")


def test_stakes_line_is_built_from_the_dial_at_creation():
    line = nick_stakes_line("48 hours")
    assert line == "🏷️ Loser surrenders their nickname for 48 hours."
    text = resolve_stakes_text("loser sings", None, nick_stake=True, nick_line=line)
    assert text == f"loser sings\n{line}"
    # A plain nickname game still persists nothing, whatever the dial says.
    assert resolve_stakes_text(None, None, nick_stake=True, nick_line=line) is None
    assert resolve_stakes_text(None, None, nick_stake=True) is None
    assert nick_stakes_line(None) == NICK_STAKES_LINE


@pytest.mark.parametrize(
    ("stored", "custom"),
    [
        pytest.param(None, None, id="plain-nickname-game"),
        pytest.param("loser sings\n🏷️ Loser surrenders their nickname for 24 hours.", "loser sings", id="custom-plus-nickname"),
        pytest.param("💰 🪙 **50** coins each — winner takes 🪙 **100** coins.", None, id="wager-only"),
        pytest.param("a\nb\n💰 x\n🏷️ Loser surrenders their nickname.", "a\nb", id="multi-line-custom"),
    ],
)
def test_custom_stakes_come_back_out_of_the_persisted_text(stored, custom):
    assert custom_stakes_from(stored) == custom


def test_no_hard_coded_durations_remain_in_member_facing_strings():
    """A static sweep over every string literal (docstrings excluded) in the
    six cogs and the shared duel modules."""
    files = [
        *(_ROOT / "cogs" / g / "cog.py" for g in (
            "chicken", "hot_potato", "hot_potato_group", "musical_chairs",
            "pressure_cooker", "quickdraw",
        )),
        *(_ROOT / "duels" / f for f in (
            "base_game.py", "base_duel.py", "filters.py", "views.py", "lobby.py", "modals.py",
        )),
    ]
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings:
                continue
            text = node.value
            if any(lit in text for lit in ("24 hours", "24h", "5 minutes", "within 5")):
                offenders.append(f"{path.name}:{node.lineno}: {text!r}")
    assert not offenders, "\n".join(offenders)


# ── refusals ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Nope.", "❌ Nope.", id="bare"),
        pytest.param("❌ Already marked.", "❌ Already marked.", id="already-prefixed"),
        pytest.param("  padded  ", "❌ padded", id="whitespace"),
    ],
)
async def test_refuse_prefixes_the_cross_exactly_once(text, expected):
    interaction = fake_interaction()
    await BaseGame._refuse(interaction, text)
    interaction.response.send_message.assert_awaited_once_with(expected, ephemeral=True)
    interaction.followup.send.assert_not_awaited()


async def test_refuse_falls_back_to_the_followup_once_the_response_is_used():
    interaction = fake_interaction()
    interaction.response.is_done = MagicMock(return_value=True)
    await BaseGame._refuse(interaction, "Too late.")
    interaction.followup.send.assert_awaited_once_with("❌ Too late.", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


async def test_sentence_refusal_blocks_the_nickname_stake_not_the_player():
    cog = _bare()
    cog.bot = SimpleNamespace(games_db=None)
    member = SimpleNamespace(id=2, display_name="Stef")
    guild = SimpleNamespace(id=1)

    async def _serving(db, guild_id, user_id):
        return {"id": 1}

    orig = duels_db.get_active_nick_for_user
    duels_db.get_active_nick_for_user = _serving  # type: ignore[assignment]
    try:
        text = await cog._check_no_active_nick(guild, [member])  # type: ignore[arg-type]
    finally:
        duels_db.get_active_nick_for_user = orig  # type: ignore[assignment]
    assert text is not None
    assert "**Stef**" in text and "nickname: False" in text
    assert "can't play again" not in text


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        pytest.param(5400, "try again in **1h 30m**", id="a-real-cooldown-names-the-time"),
        pytest.param(None, "try again later", id="the-timeless-form"),
    ],
)
def test_lobby_cooldown_copy(remaining, expected):
    text = _bare()._cooldown_copy(remaining)
    assert text.startswith("You're on cooldown for this game")
    assert expected in text


def test_launch_refusal_copy_points_at_the_dashboard():
    """A denial that just says no is a dead end (embed_style_guide.md)."""
    cog = _bare()
    cog.bot = SimpleNamespace(games_db=None)

    async def _enabled(db, game_type, guild_id):
        return True

    import bot_modules.duels.base_game as bg

    orig = bg.check_game_enabled
    bg.check_game_enabled = _enabled  # type: ignore[assignment]
    try:
        import asyncio

        text = asyncio.run(cog._launch_refusal(1, 5, {"channel_allowlist": "[7]"}))
    finally:
        bg.check_game_enabled = orig  # type: ignore[assignment]
    assert text is not None and "dashboard" in text


def test_refuse_is_deliberately_not_a_style_contract_wrapper():
    """The contract sweep expects a wrapper to forward its literal verbatim;
    ``_refuse`` adds the prefix itself, so listing it there would flag every
    caller. This test is the guarantee instead."""
    from tests.test_embed_style_contract import _SEND_WRAPPERS

    assert "_refuse" not in _SEND_WRAPPERS
