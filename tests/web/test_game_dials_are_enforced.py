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

import inspect
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


def test_the_rematch_cooldown_dial_is_read_by_both_game_shapes() -> None:
    """duels-party-116: 'Wait Before a Rematch' sat on the three duel panels
    and was read by nothing, while the group games enforced the same dial at
    48 hours. Both shapes read it now, and it ships at 0 — no cooldown is
    what every duel behaved like before."""
    from bot_modules.duels import db as duels_db
    from web_server.routes.config import _DUEL_SHARED_DEFAULTS

    duel = (_ROOT / "src" / "bot_modules" / "duels" / "base_duel.py").read_text(encoding="utf-8")
    group = (_ROOT / "src" / "bot_modules" / "duels" / "base_game.py").read_text(encoding="utf-8")
    assert "duels_db.check_cooldown(" in duel and 'cfg["cooldown_hours"]' in duel
    assert "duels_db.check_group_cooldown(" in group and 'cfg["cooldown_hours"]' in group
    assert duels_db._CONFIG_DEFAULTS["cooldown_hours"] == 0
    assert _DUEL_SHARED_DEFAULTS["cooldown_hours"] == 0


@pytest.mark.parametrize("stem", sorted(DUEL_PANELS))
def test_the_rematch_cooldown_hint_says_it_guards_the_nickname_stake(stem: str) -> None:
    """The dial only holds back nickname games; a hint promising to stop
    'the same two people' playing at all would be a lie for a wagered rematch."""
    src = _duel_panel(stem)
    hint = re.search(r'numField\("cooldown_hours".*?"([^"]*nickname[^"]*)"', src, re.S)
    assert hint, f"config-games-{stem}.js's cooldown hint doesn't say it is nickname-only"
    assert "never held back" in hint.group(1)


# ── Per-game mechanics dials that replaced or joined a table (2026-09-04) ────
# The duel panels' numeric dials are numField(...) calls PUT to a
# /api/config/games-* route and read back through each game's db.get_config.
# Chicken's "Climb Time" became the Earliest/Latest Crash pair (the crash is
# rolled between them and hidden), and Hot Potato's duel gained the group
# cog's Shortest Hold. Each has to be a key the cog reads, or it is inert.


def _duel_num_fields(stem: str) -> list[str]:
    return re.findall(r'numField\("([a-z_]+)"', _duel_panel(stem))


@pytest.mark.parametrize(
    ("stem", "dials", "reader"),
    [
        pytest.param("chicken", ["min_climb", "max_climb"], "chicken/cog.py", id="chicken-crash-range"),
        pytest.param("hotpotato", ["min_hold"], "hot_potato/cog.py", id="hot-potato-min-hold"),
    ],
)
def test_new_mechanics_dials_are_offered_and_read(stem: str, dials: list[str], reader: str) -> None:
    from web_server.routes.config import _DUEL_GAMES

    offered = _duel_num_fields(stem)
    cog = (_COGS / reader).read_text(encoding="utf-8")
    game_key = DUEL_PANELS[stem][1]
    for dial in dials:
        assert dial in offered, f"config-games-{stem}.js no longer offers {dial}"
        assert dial in _DUEL_GAMES[game_key]["fields"], f"the API cannot write {dial}"
        assert f'cfg["{dial}"]' in cog, f"{reader} never reads {dial}"


def test_chicken_no_longer_offers_a_fixed_public_climb_time() -> None:
    """duels-party-113: a fixed climb_duration was a public crash point."""
    from web_server.routes.config import _DUEL_GAMES

    assert "climb_duration" not in _duel_num_fields("chicken")
    assert "climb_duration" not in _DUEL_GAMES["chicken"]["fields"]


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
    # Risky Rolls keeps its own settings panel, but it is in the availability
    # list like every other game, so /risky start has to honour the switch too.
    "risky_roll": "risky_roll_cog.py",
}


@pytest.mark.parametrize("game_type", sorted(STARTABLE))
def test_every_toggleable_game_gates_its_own_start(game_type: str) -> None:
    src = (_COGS / STARTABLE[game_type]).read_text(encoding="utf-8")
    # The cogs reach their GamesDb differently — most hold `self.db`, Risky
    # Rolls builds one from the app context — so match the call, not the handle.
    # ``launch_refusal`` (games/utils/launch_guard.py) is the shared guard
    # that runs check_game_enabled for the entries wired through it.
    called = re.search(
        r'(?:check_game_enabled|launch_refusal)\(\s*[^,]+,\s*"' + re.escape(game_type) + '"', src
    )
    assert called, (
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
    # Every schedulable type is either addressable itself or a display variant
    # of one that is (ffa_banner shares ffa's switch).
    from bot_modules.games.constants import SCHEDULE_BASE_GAME_TYPE

    for gt in SCHEDULABLE_GAME_TYPES:
        base = SCHEDULE_BASE_GAME_TYPE.get(gt, gt)
        assert base in ALL_GAME_TYPES, (
            f"scheduled launches of {gt} check an enable switch nothing can set"
        )


# ── The Game Night ping dial ────────────────────────────────────────────────
# A role dial on Games Global Config is only honest if the sweep that posts
# the ping reads the same key, through the same registry entry.


def test_game_night_ping_dial_is_the_key_the_sweep_pings() -> None:
    from bot_modules.services.feature_roles import GAME_NIGHT_PING

    assert GAME_NIGHT_PING.key == "game_night_ping_role_id"
    assert GAME_NIGHT_PING.panel == "games-config" and GAME_NIGHT_PING.opt_in
    panel = (_PANELS / "games-config.js").read_text(encoding="utf-8")
    assert "game_night_ping_role_id" in panel
    route = (_ROOT / "src" / "web_server" / "routes" / "games.py").read_text(encoding="utf-8")
    assert "GAME_NIGHT_PING.key" in route
    sweep = (_ROOT / "src" / "bot_modules" / "services" / "game_start_ping_service.py").read_text(encoding="utf-8")
    assert "GAME_NIGHT_PING.key" in sweep and "role_only_mentions" in sweep


# ── the casino panel (Economy → Casino) ───────────────────────────────
#
# Not an optSchema panel: its dials are numInput/checkbox names PUT to
# /api/config/casino and read back through ``CasinoSettings``. Same rule
# though — every name the panel offers must be a field the service loads,
# and the copy must describe what the broadcast does now.

_CASINO_PANEL = _PANELS / "config-casino.js"


def _casino_panel_dials() -> list[str]:
    src = _CASINO_PANEL.read_text(encoding="utf-8")
    return re.findall(r'(?:numInput|checkbox)\(\s*"([a-z_]+)"', src)


def test_every_casino_panel_dial_is_a_setting_the_service_loads() -> None:
    from dataclasses import fields

    from bot_modules.services.casino_service import CasinoSettings

    dials = _casino_panel_dials()
    assert dials, "the casino panel declares no dials — did the regex rot?"
    known = {f.name for f in fields(CasinoSettings)}
    unread = [d for d in dials if d not in known]
    assert not unread, f"config-casino.js offers dials nothing loads: {unread}"


def test_the_casino_broadcast_multiple_is_a_panel_dial() -> None:
    """D6 (2026-09-02): the big-win broadcast needs a real multiple, and the
    multiple is an admin dial, not a constant."""
    assert "broadcast_min_mult" in _casino_panel_dials()


def test_the_casino_daily_comp_is_a_dial_that_ships_dark_and_is_enforced() -> None:
    """casino-134 (2026-09-04): the return hook is a panel dial, it defaults
    to off, and both the hub button and the claim itself read it — an
    admin who never touches the panel gets no comp, and one who sets it
    gets exactly one spin per member per day."""
    from bot_modules.cogs.casino.views import build_hub_view
    from bot_modules.services.casino_service import (
        DEFAULT_CASINO_SETTINGS,
        CasinoSettings,
        claim_daily_comp,
        comp_on,
    )

    assert "daily_comp" in _casino_panel_dials()
    assert DEFAULT_CASINO_SETTINGS.daily_comp == 0
    assert not comp_on(DEFAULT_CASINO_SETTINGS) and comp_on(CasinoSettings(daily_comp=5))
    hub_ids = {
        getattr(item, "custom_id", "") for item in build_hub_view(DEFAULT_CASINO_SETTINGS).children
    }
    assert "casino:comp" not in hub_ids
    assert "casino:comp" in {
        getattr(item, "custom_id", "")
        for item in build_hub_view(CasinoSettings(daily_comp=5)).children
    }
    # The claim reads the dial itself, so a stale panel cannot bypass it.
    src = inspect.getsource(claim_daily_comp)
    assert "settings.daily_comp" in src and "comp_claimed" in src


def test_the_casino_panel_no_longer_promises_a_play_again_button() -> None:
    """casino-139: the public recap lost its buttons in August; the dial's
    help text still said the broadcast came "with a Play Again button"."""
    src = _CASINO_PANEL.read_text(encoding="utf-8")
    assert "Play Again" not in src
    assert "carries no buttons" in src
