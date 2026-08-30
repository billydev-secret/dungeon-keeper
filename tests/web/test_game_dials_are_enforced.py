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


# ── Duel panels: the promises they make about being switched off ─────────────
# All six duel panels used to open with "No channels are allowed to host party
# games yet, so this game cannot be played anywhere", and their Allowed Channels
# hint pointed at "every channel that may host party games". Both describe the
# global games_allowed_channels list, which no duel or lobby code path reads —
# with an empty per-game allowlist a duel game runs everywhere. The banner was
# also the only off switch these games were ever advertised as having.

# panel stem -> (cog module path, GAME_KEY)
DUEL_PANELS = {
    "pressure": ("pressure_cooker/cog.py", "pressure"),
    "quickdraw": ("quickdraw/cog.py", "quickdraw"),
    "hotpotato": ("hot_potato/cog.py", "hot_potato"),
    "hotpotatogroup": ("hot_potato_group/cog.py", "hot_potato_group"),
    "chicken": ("chicken/cog.py", "chicken"),
    "musicalchairs": ("musical_chairs/cog.py", "musical_chairs"),
}


def _duel_panel(stem: str) -> str:
    return (_PANELS / f"config-games-{stem}.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("stem", sorted(DUEL_PANELS))
def test_duel_panel_does_not_blame_the_global_games_channel_list(stem: str) -> None:
    src = _duel_panel(stem)
    for lie in ("may host party games", "cannot be played anywhere"):
        assert lie not in src, (
            f"config-games-{stem}.js still tells admins the Games › Global Config "
            f"channel list governs this game; no duel code path reads it"
        )


@pytest.mark.parametrize("stem", sorted(DUEL_PANELS))
def test_duel_panel_offers_an_enable_toggle_under_the_cogs_game_key(stem: str) -> None:
    """The toggle must write the key the cog's enable check reads."""
    cog_path, game_key = DUEL_PANELS[stem]
    cog = (_COGS / cog_path).read_text(encoding="utf-8")
    assert f'GAME_KEY = "{game_key}"' in cog

    src = _duel_panel(stem)
    assert "mountGamePanel(" in src, f"config-games-{stem}.js has no enable toggle"
    assert f'gameType: "{game_key}"' in src


@pytest.mark.parametrize("stem", sorted(DUEL_PANELS))
def test_duel_panel_exposes_the_nickname_denylist(stem: str) -> None:
    """nick_denylist is enforced on every nickname and every line of stakes
    text; before this it could only be set by editing the database."""
    src = _duel_panel(stem)
    assert 'name="nick_denylist"' in src
    assert "payload.nick_denylist" in src


# ── A bank is a dial too ────────────────────────────────────────────────────
# The same rule one level up: a panel that offers a question bank promises the
# rows curated there will be served. AMA's panel offered the whole
# add/bulk/pool/tags UI for a game whose questions only ever come from members'
# own submissions — no draw function ever read an AMA bank, so everything
# curated there was stranded.

_SOURCES = [
    _ROOT / "src" / "bot_modules" / "games" / "utils" / "question_source.py",
    _COGS / "pen_pals_cog.py",
]


def _bank_panels() -> dict[str, str]:
    """panel filename -> gameType, for every panel that mounts a bank."""
    out = {}
    for path in sorted(_PANELS.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        if path.name == "games-panel-shared.js" or "hasBank: true" not in src:
            continue
        m = re.search(r'gameType:\s*"([a-z_]+)"', src)
        if m:
            out[path.name] = m.group(1)
    return out


def test_ffa_bank_states_its_reserved_tags() -> None:
    """FFA's draw treats 'truth' and 'dare' as a required dimension: a truth
    round serves only rows tagged truth. The panel was plain free-tag mode with
    the generic hint, so a curator had no way to know an untagged question
    would only ever come up in a random round."""
    source = _SOURCES[0].read_text(encoding="utf-8")
    assert '{"truth"}' in source and '{"dare"}' in source, (
        "the reserved-tag filter this hint documents has moved — re-check the hint"
    )
    hint = (_PANELS / "games-ffa.js").read_text(encoding="utf-8")
    assert "<strong>truth</strong>" in hint and "<strong>dare</strong>" in hint


def test_every_bank_panel_is_drawn_by_the_bot() -> None:
    drawn = "".join(p.read_text(encoding="utf-8") for p in _SOURCES)
    stranded = {
        panel: gt for panel, gt in _bank_panels().items() if f'"{gt}"' not in drawn
    }
    assert not stranded, (
        "these panels curate a question bank no draw function ever reads, so "
        f"every question added there is stranded: {stranded}"
    )


# ── Per-server option defaults need a dashboard writer ──────────────────────
# A cog reading a stored option is only honest if some panel can write it.
# Two Truths & a Lie read a server-level vote_timer default with no panel at
# all, and Rushmore read a 'mode' default its panel never offered.

# Types whose options are written somewhere other than a games optSchema.
# Photo Challenge's options (channel_id, ping_role_id) come from its own panel
# through /api/photo-challenge.
_OPTIONS_WRITTEN_ELSEWHERE = {"photo"}


def test_every_stored_option_a_cog_reads_has_a_panel_dial() -> None:
    missing: dict[str, list[str]] = {}
    for cog_path in sorted(_COGS.glob("games_*_cog.py")):
        src = cog_path.read_text(encoding="utf-8")
        m = re.search(r'get_game_options\(self\.db,\s*"([a-z_]+)"', src)
        if not m:
            continue
        game_type = m.group(1)
        if game_type in _OPTIONS_WRITTEN_ELSEWHERE:
            continue
        keys = sorted(set(re.findall(r'game_opts\.get\(\s*"([a-z_]+)"', src)))
        if not keys:
            continue
        panel = _PANELS / f"games-{game_type}.js"
        dials = _dials(game_type) if panel.exists() else []
        unwritable = [k for k in keys if k not in dials]
        if unwritable:
            missing[game_type] = unwritable
    assert not missing, (
        "these per-server defaults are read by a cog but no dashboard panel can "
        f"set them: {missing}"
    )


# ── A game you cannot switch off ────────────────────────────────────────────
# The dashboard's per-game "Available on This Server" switch is only true if
# the game actually consults it. LegitLibs and the six schedule-first games
# never did, and two of them could not even be addressed: the config API spelt
# Risky Rolls 'risky_roller' and did not list LegitLibs at all.

# game_type -> the cog file that owns its start command.
STARTABLE = {
    "wyr": "games_wyr_cog.py",
    "nhie": "games_nhie_cog.py",
    "mlt": "games_mlt_cog.py",
    "rushmore": "games_rushmore_cog.py",
    "price": "games_price_cog.py",
    "clapback": "games_clapback_cog.py",
    "ama": "games_ama_cog.py",
    "traditional": "games_traditional_cog.py",
    "ffa": "games_ffa_cog.py",
    "mfk": "games_mfk_cog.py",
    "compliment": "games_compliment_cog.py",
    "ttl": "games_ttl_cog.py",
    "hottakes": "games_hottakes_cog.py",
    "story": "games_story_cog.py",
    "fantasies": "games_fantasies_cog.py",
    "legitlibs": "games_legitlibs/__init__.py",
}


@pytest.mark.parametrize("game_type", sorted(STARTABLE))
def test_every_toggleable_game_gates_its_own_start(game_type: str) -> None:
    src = (_COGS / STARTABLE[game_type]).read_text(encoding="utf-8")
    assert f'check_game_enabled(self.db, "{game_type}"' in src, (
        f"{STARTABLE[game_type]} never checks the per-guild enable switch, so "
        f"turning {game_type} off on the dashboard would change nothing"
    )


def test_the_config_api_knows_every_game_it_can_switch_off() -> None:
    """The API's list is what the dashboard can address. A type missing from it
    404s; a type spelt differently from the bot's own name writes a row nothing
    will ever read."""
    from bot_modules.games.constants import GAME_NAMES, SCHEDULABLE_GAME_TYPES
    from web_server.routes.games import ALL_GAME_TYPES

    unknown = [gt for gt in ALL_GAME_TYPES if gt not in GAME_NAMES]
    assert not unknown, f"config API offers game types the bot has no name for: {unknown}"

    # "photo" is excluded on purpose: Photo Challenge owns its games_game_config
    # row from its own standalone panel (PUT /api/photo-challenge/config), and
    # listing it here as well gave that one row two live write paths. It is
    # switchable — just not from this list.
    unreachable = [gt for gt in STARTABLE if gt not in ALL_GAME_TYPES]
    assert not unreachable, (
        f"these games cannot be switched off from the dashboard at all: {unreachable}"
    )
    # Every schedulable type either is addressable or is a display variant of
    # one that is (ffa_banner shares ffa's switch), or it is a game configured
    # from its own panel rather than the shared games config.
    from bot_modules.games.constants import SCHEDULE_BASE_GAME_TYPE

    own_panel = {"risky_roll"}
    for gt in SCHEDULABLE_GAME_TYPES:
        base = SCHEDULE_BASE_GAME_TYPE.get(gt, gt)
        assert base in ALL_GAME_TYPES or base in own_panel, (
            f"scheduled launches of {gt} check an enable switch nothing can set"
        )
