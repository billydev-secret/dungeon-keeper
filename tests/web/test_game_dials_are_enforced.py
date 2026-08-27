"""A dial on a game panel must be a setting the bot actually reads.

CLAUDE.md: "Never ship a preference or toggle that isn't enforced." Seven
panels declared 27 per-game dials through `optSchema`; 17 of them were stored
and read by nothing. Four panels — WYR, AMA, MLT and NHIE — never called
`get_game_options` at all, so every dial on them was inert.

The subtle ones were worse than merely dead:

  * WYR's "Hide Who Voted for What" could not have done what it said. Naming
    voters is driven by a separate `revealed` flag set by a host/mod button;
    `anonymous` only gated whether per-vote audit rows were written. Wiring it
    as labelled would have built an audit-suppression switch that hid nothing
    from members.
  * AMA's key was `screened`; the cog reads `mode`. They could never meet.
  * Clapback's "Include NSFW Prompts" contradicted a house rule — NSFW gates on
    `channel.is_nsfw()`, never a bot-side toggle — and the cog was right to
    overwrite it with the channel's own age-gate.

This pins the outcome: every remaining dial names a key its cog reads, and the
two games with a join phase enforce their player limits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PANELS = _ROOT / "src" / "web_server" / "static" / "js" / "panels"
_COGS = _ROOT / "src" / "bot_modules" / "cogs"

# panel stem -> cog file. Every game panel that declares dials.
GAMES = {
    "wyr": "games_wyr_cog.py",
    "ama": "games_ama_cog.py",
    "mlt": "games_mlt_cog.py",
    "nhie": "games_nhie_cog.py",
    "price": "games_price_cog.py",
    "rushmore": "games_rushmore_cog.py",
    "clapback": "games_clapback_cog.py",
}

# Dials deleted because nothing read them, with why. Each must stay gone.
RETIRED = {
    "wyr": ["anonymous", "min_players", "max_players"],
    "ama": ["screened", "min_players", "max_players"],
    "nhie": ["lives", "min_players", "max_players"],
    "price": ["min_players", "max_players"],
    "rushmore": ["draft_rounds"],
    "clapback": ["allow_nsfw"],
}


def _dials(game: str) -> list[str]:
    src = (_PANELS / f"games-{game}.js").read_text(encoding="utf-8")
    m = re.search(r"optSchema:\s*\[(.*?)\n\s*\],", src, re.S)
    if not m:
        return []
    return re.findall(r'\{\s*key:\s*"([a-z_]+)"', m.group(1))


def _cog(game: str) -> str:
    return (_COGS / GAMES[game]).read_text(encoding="utf-8")


@pytest.mark.parametrize("game", sorted(GAMES))
def test_every_dial_names_a_key_its_cog_reads(game: str) -> None:
    dials = _dials(game)
    if not dials:
        return
    cog = _cog(game)
    assert "get_game_options" in cog, (
        f"games-{game}.js declares {dials} but {GAMES[game]} never loads stored "
        "options, so none of them can take effect"
    )
    unread = [d for d in dials if f'"{d}"' not in cog]
    assert not unread, (
        f"games-{game}.js declares dials its cog never reads: {unread}"
    )


@pytest.mark.parametrize("game", sorted(RETIRED))
def test_retired_dials_stay_retired(game: str) -> None:
    still_there = [d for d in _dials(game) if d in RETIRED[game]]
    assert not still_there, (
        f"games-{game}.js has readded dials nothing enforces: {still_there}"
    )


def test_only_the_games_with_a_lobby_offer_player_limits() -> None:
    """A floor or a ceiling needs a join phase to be enforced in. MLT and
    Rushmore create their game with state="joining"; the rest go straight to
    state="playing", so a player limit there has nowhere to apply."""
    with_limits = {g for g in GAMES if {"min_players", "max_players"} & set(_dials(g))}
    assert with_limits == {"mlt", "rushmore"}, (
        f"player limits offered where there is no lobby: {with_limits}"
    )
    for game in with_limits:
        assert 'state="joining"' in _cog(game), f"{game} has no join phase"


def test_clapback_does_not_offer_an_nsfw_toggle() -> None:
    """CLAUDE.md: NSFW gates on channel.is_nsfw(), Discord's own age-gate,
    never a bot-side toggle. The cog overwrites any stored value with
    channel_allows_nsfw(channel), which is the correct behaviour."""
    assert "allow_nsfw" not in _dials("clapback")
    assert "channel_allows_nsfw(channel)" in _cog("clapback")


def test_wyr_reveal_voters_is_documented() -> None:
    """The deleted dial implied votes could be hidden. They cannot: a host or
    mod can name every voter with a button, and that had never been written
    down anywhere a member or admin would read."""
    manual = (_ROOT / "src" / "web_server" / "static" / "manual.html").read_text(encoding="utf-8")
    assert "Reveal Voters" in manual
    assert "Reveal Voters" in _cog("wyr")
