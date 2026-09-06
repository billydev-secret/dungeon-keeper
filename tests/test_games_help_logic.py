"""Tests for the Games Help modules.

Covers ``bot_modules/games_help/logic.py`` (command and description
lookups, alignment guarantees against the canonical ``GAME_ICONS``
registry, the grouped overview and the per-game detail), the play registry
in ``games/constants.py`` that feeds both the panel and the ``/games play``
picker descriptions (discovery-13), and ``bot_modules/games_help/embeds.py``
(the overview, detail and ``/support`` embeds).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bot_modules.games.constants import (
    AUTO_START_LOBBY_TYPES,
    DUEL_GAME_KEYS,
    GAME_ICONS,
    GAME_MIN_PLAYERS,
    GAME_NAMES,
    GAME_TYPICAL_LENGTH,
    HOW_TO_PLAY,
    LOBBY_GAME_TYPES,
    LOBBY_MIN_PLAYERS,
    hosting_kind,
    play_description,
)
from bot_modules.games_help.embeds import (
    build_game_detail_embed,
    build_help_embed,
    build_support_embed,
)
from bot_modules.games_help.logic import (
    EMBED_FIELD_LIMIT,
    FIELD_VALUE_LIMIT,
    FOLDED_VARIANTS,
    GAME_COMMANDS,
    GAME_DESCRIPTIONS,
    OTHER_COMMANDS_VALUE,
    SELECT_OPTION_LIMIT,
    SUPPORT_INVITE_URL,
    chunk_lines,
    game_detail,
    help_groups,
    room_help_lines,
    select_options,
    survivor_help_line,
)

_DOCS_ROOT = Path(__file__).resolve().parents[1]


# ── alignment guarantees ─────────────────────────────────────────────


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_every_game_icon_has_a_command(key):
    """Each entry in GAME_ICONS (except the internal-only ``pressure``
    key) must have a slash-command listed — otherwise the help embed
    silently falls back to ``"/<key>"``."""
    assert key in GAME_COMMANDS, f"GAME_COMMANDS missing entry for {key!r}"


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_every_game_icon_has_a_description(key):
    """Each entry in GAME_ICONS (except ``pressure``) must have a
    description so the help embed never renders a blank tail."""
    assert key in GAME_DESCRIPTIONS, (
        f"GAME_DESCRIPTIONS missing entry for {key!r}"
    )


def test_no_orphan_command_entries():
    """Every key in GAME_COMMANDS should correspond to a real game."""
    for key in GAME_COMMANDS:
        assert key in GAME_ICONS, f"GAME_COMMANDS has orphan {key!r}"


def test_no_orphan_description_entries():
    for key in GAME_DESCRIPTIONS:
        assert key in GAME_ICONS, f"GAME_DESCRIPTIONS has orphan {key!r}"


def test_all_commands_start_with_slash():
    for key, cmd in GAME_COMMANDS.items():
        assert cmd.startswith("/"), f"{key} command {cmd!r} missing leading /"


def test_support_invite_url_is_discord_link():
    assert SUPPORT_INVITE_URL.startswith("https://discord.gg/")


def test_other_commands_value_references_recap_and_support():
    """The block names ``/support`` — the top-level command that exists — not
    the ``/games support`` subcommand it advertised for months (platform-25)."""
    assert "/recap" in OTHER_COMMANDS_VALUE
    assert "/support" in OTHER_COMMANDS_VALUE
    assert "/games support" not in OTHER_COMMANDS_VALUE


# ── the play registry (discovery-13) ─────────────────────────────────


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_every_game_has_a_floor_and_a_length(key):
    assert key in GAME_MIN_PLAYERS, f"GAME_MIN_PLAYERS missing {key!r}"
    assert key in GAME_TYPICAL_LENGTH, f"GAME_TYPICAL_LENGTH missing {key!r}"


@pytest.mark.parametrize("key", sorted(LOBBY_GAME_TYPES))
def test_lobby_floors_agree_with_the_sweep(key):
    """The floor the help panel quotes is the one the idle-lobby sweep
    enforces — except Fantasies, whose sweep floor of one is a "somebody
    opened the panel" fact, not a player count."""
    if key == "fantasies":
        assert GAME_MIN_PLAYERS[key] >= LOBBY_MIN_PLAYERS[key]
    else:
        assert GAME_MIN_PLAYERS[key] == LOBBY_MIN_PLAYERS[key]


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_play_description_fits_discord_and_names_the_floor(key):
    """≤100 chars is Discord's ceiling on a command description; the floor
    and the name are the two things a member scanning the picker needs."""
    desc = play_description(key)
    assert len(desc) <= 100, f"{key}: {len(desc)} chars"
    assert desc.startswith(GAME_NAMES[key])
    floor = GAME_MIN_PLAYERS[key]
    assert ("any number of players" if floor <= 1 else f"{floor}+ players") in desc


def _play_subcommands():
    """Every ``/games play`` subcommand as its cog class declares it — the
    cogs attach them to the shared ``play`` group only when added to a bot,
    so read the class-level list discord.py builds at import."""
    import importlib
    import inspect

    from discord.ext import commands

    for mod in (
        "games_ama_cog", "games_clapback_cog", "games_compliment_cog",
        "games_fantasies_cog", "games_ffa_cog", "games_hottakes_cog",
        "games_mfk_cog", "games_mlt_cog", "games_nhie_cog", "games_price_cog",
        "games_rushmore_cog", "games_story_cog", "games_traditional_cog",
        "games_ttl_cog", "games_wyr_cog", "games_legitlibs",
    ):
        module = importlib.import_module(f"bot_modules.cogs.{mod}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                yield from cls.__cog_app_commands__


def test_every_play_subcommand_description_comes_from_the_registry():
    """Each ``/games play`` description must be the registry's line, so the
    picker and the help panel can never disagree about a floor."""
    by_command = {cmd.split()[-1]: key for key, cmd in GAME_COMMANDS.items()
                  if cmd.startswith("/games play ")}
    seen = set()
    for cmd in _play_subcommands():
        key = by_command.get(cmd.name)
        assert key is not None, f"/games play {cmd.name} has no GAME_COMMANDS entry"
        assert cmd.description == play_description(key), cmd.name
        seen.add(key)
    assert seen == set(by_command.values())


# ── the hosting registry (P4 leftover: a scheduled Clapback starts itself) ─


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        pytest.param("ffa", "self", id="prompt-card"),
        pytest.param("wyr", "timer", id="timer-round"),
        pytest.param("clapback", "countdown", id="clapback-auto-start"),
        pytest.param("price", "countdown", id="price-auto-start"),
        pytest.param("mlt", "host", id="plain-lobby"),
        pytest.param("ama", "host", id="host-driven"),
    ],
)
def test_hosting_kind_follows_the_registry(key, kind):
    assert hosting_kind(key) == kind


def test_auto_start_registry_mirrors_the_cogs():
    """``AUTO_START_LOBBY_TYPES`` is a static mirror of the cogs that
    register a ``lobby_auto_starters`` entry in ``setup()``; a cog gaining
    or losing one must move its tag too."""
    cogs = _DOCS_ROOT / "src" / "bot_modules" / "cogs"
    registered = set()
    for path in cogs.rglob("*.py"):
        registered.update(
            re.findall(
                r"lobby_auto_starters\[\"(\w+)\"\]", path.read_text(encoding="utf-8")
            )
        )
    assert registered == set(AUTO_START_LOBBY_TYPES)


# ── copy contradictions (discovery-8) ────────────────────────────────


def test_legitlibs_help_names_classic_as_the_default():
    """The slash entry defaults ``mode`` to classic; the in-game Help said
    Quiplash was the default."""
    text = HOW_TO_PLAY["legitlibs"]
    assert "Classic mode (default)" in text
    assert "Quiplash mode (default)" not in text


def test_ttl_floor_matches_the_start_guard():
    """The picker line and help card say what Start Guessing enforces (T1)."""
    from bot_modules.games_ttl.logic import MIN_PLAYERS

    assert GAME_MIN_PLAYERS["ttl"] == MIN_PLAYERS


def test_mahjong_is_named_but_not_on_the_menu():
    """Settled hands write history rows as 'mahjong' (Play Statistics needs
    a display name); the table is a room, not a /games play launch (M3)."""
    assert GAME_NAMES["mahjong"] == "Meadow Mahjong"
    assert "mahjong" not in GAME_ICONS


def test_photo_has_no_dead_how_to_play():
    """Photo Challenge left the games menu; its HOW_TO_PLAY entry was text
    nothing rendered."""
    assert "photo" not in HOW_TO_PLAY
    assert "photo" not in GAME_ICONS


# ── doc-count tripwires ──────────────────────────────────────────────
# The feature map and web manual advertise the party-game count in prose.
# These numbers have drifted twice (16 → 17 → 18); fail loudly when a game is
# added to GAME_ICONS without updating the docs. The prose list used to live
# in README.md and moved to docs/features.md 2026-09-03 when the README became
# a pitch — the tripwire follows the list, not the filename.

_PARTY_GAME_KEYS = [k for k in GAME_ICONS if k not in DUEL_GAME_KEYS]


def test_features_doc_party_game_count_matches_code():
    features = (_DOCS_ROOT / "docs" / "features.md").read_text(encoding="utf-8")
    expected = f"{len(_PARTY_GAME_KEYS)}-game"
    assert expected in features, (
        f"docs/features.md should say '{expected}' (GAME_ICONS has "
        f"{len(_PARTY_GAME_KEYS)} party games) — update the count."
    )


def test_manual_party_game_count_matches_code():
    manual = (
        _DOCS_ROOT / "src" / "web_server" / "static" / "manual.html"
    ).read_text(encoding="utf-8")
    expected = f"{len(_PARTY_GAME_KEYS)} party games"
    assert expected in manual, (
        f"manual.html's Feature Map should say '{expected}' — update the count."
    )


# ── help_groups / select_options / game_detail ───────────────────────


def test_help_groups_cover_every_game_once():
    groups = dict(help_groups())
    listed = "\n".join(line for lines in groups.values() for line in lines)
    for key in GAME_ICONS:
        if key in FOLDED_VARIANTS:
            assert GAME_COMMANDS[key] not in listed, "folded variant listed on its own"
            continue
        assert f"`{GAME_COMMANDS[key]}`" in listed, f"{key} missing from the overview"
    party = "\n".join(groups["🎉 Party Games"])
    duels = "\n".join(groups["⚔️ Duels & Group Games"])
    for key in DUEL_GAME_KEYS:
        assert GAME_COMMANDS[key] in duels and GAME_COMMANDS[key] not in party


def test_help_groups_rooms_carry_extra_lines_and_whisper():
    rooms = dict(help_groups(["🏈 **Survivor** — open in <#5>"]))["🏠 Rooms & Tables"]
    assert rooms[0] == "🏈 **Survivor** — open in <#5>"
    assert any("Whisper" in line for line in rooms)


def test_select_options_fold_the_banner_and_fit_the_menu():
    keys = [k for k, _, _ in select_options()]
    assert "ffa" in keys and "ffa_banner" not in keys
    assert len(keys) == len(GAME_ICONS) - len(FOLDED_VARIANTS)
    assert len(keys) <= SELECT_OPTION_LIMIT
    assert all(len(label) <= 100 and len(desc) <= 100 for _, label, desc in select_options())


def test_game_detail_reads_the_registry():
    d = game_detail("clapback")
    assert d.rules == HOW_TO_PLAY["clapback"]
    assert d.floor == "3+ players"
    assert d.hosting_label == "Starts itself after a countdown"
    assert d.length == GAME_TYPICAL_LENGTH["clapback"]
    assert d.command == "/games play clapback"
    assert d.startable is True


def test_game_detail_folds_the_banner_into_ffa():
    d = game_detail("ffa")
    assert any("/games play ffa_banner" in line for line in d.variant_lines)
    assert game_detail("wyr").variant_lines == ()


@pytest.mark.parametrize("key", sorted(DUEL_GAME_KEYS))
def test_duels_are_not_startable_from_the_panel(key):
    """A duel needs an opponent picked at the command — no headless
    ``launch()`` to press."""
    assert game_detail(key).startable is False


def test_chunk_lines_splits_under_the_field_limit():
    lines = ["x" * 400] * 4
    chunks = chunk_lines(lines, limit=1024)
    assert len(chunks) == 2
    assert all(len(c) <= 1024 for c in chunks)
    assert chunk_lines([]) == []
    assert chunk_lines(["a", "b"]) == ["a\nb"]


# ── build_help_embed ─────────────────────────────────────────────────


def test_build_help_embed_has_title_and_description():
    embed = build_help_embed()
    assert embed.title is not None
    assert "Community Games" in embed.title
    assert embed.description is not None
    assert "Start Here" in embed.description


def test_build_help_embed_lists_every_game_in_a_group():
    """One line per game inside its group — not one field per game."""
    embed = build_help_embed()
    body = "\n".join(f.value or "" for f in embed.fields)
    for key in GAME_ICONS:
        if key in FOLDED_VARIANTS:
            continue
        assert f"{GAME_ICONS[key]} **{GAME_NAMES[key]}**" in body, f"missing line for {key}"
    names = [f.name for f in embed.fields]
    assert names[:3] == ["🎉 Party Games", "⚔️ Duels & Group Games", "🏠 Rooms & Tables"]


def test_build_help_embed_stays_under_discords_ceilings():
    """The tripwire discovery-7 asked for: the next game added fails here,
    not at send time. Fields ≤ 25 and every value ≤ 1024."""
    embed = build_help_embed(extra_lines=[f"line {i}" for i in range(10)])
    assert len(embed.fields) <= EMBED_FIELD_LIMIT
    assert all(len(f.value or "") <= FIELD_VALUE_LIMIT for f in embed.fields)


def test_build_help_embed_includes_other_commands_section():
    embed = build_help_embed()
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "⚙️ Other Commands" in by_name
    assert "/recap" in by_name["⚙️ Other Commands"]
    assert "/games support" not in by_name["⚙️ Other Commands"]


def test_build_help_embed_has_footer():
    embed = build_help_embed()
    assert embed.footer.text is not None
    assert "/games help" in embed.footer.text


def test_build_help_embed_uses_golden_meadow_color():
    from bot_modules.games.constants import BRAND_COLOR

    embed = build_help_embed()
    assert embed.color is not None
    assert embed.color.value == BRAND_COLOR


# ── build_game_detail_embed ──────────────────────────────────────────


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_build_game_detail_embed_for_every_game(key):
    embed = build_game_detail_embed(key)
    assert embed.title == f"{GAME_ICONS[key]} {GAME_NAMES[key]}"
    assert embed.description  # the rules, or at least the one-liner
    assert len(embed.fields) <= EMBED_FIELD_LIMIT
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["👥 Players"].startswith(
        "any number" if GAME_MIN_PLAYERS[key] <= 1 else f"{GAME_MIN_PLAYERS[key]}+"
    )
    assert f"`{GAME_COMMANDS[key]}`" in by_name["▶️ Start With"]
    assert all(len(f.value or "") <= FIELD_VALUE_LIMIT for f in embed.fields)


def test_build_game_detail_embed_pacing_names_the_hosting_kind():
    by_name = {f.name: f.value or "" for f in build_game_detail_embed("wyr").fields}
    assert by_name["🎛️ Pacing"].startswith("Self-running with a round timer")


# ── build_support_embed ──────────────────────────────────────────────


def test_build_support_embed_has_title():
    embed = build_support_embed()
    assert embed.title is not None
    assert "Support" in embed.title


def test_build_support_embed_includes_invite_url():
    embed = build_support_embed()
    assert embed.description is not None
    assert SUPPORT_INVITE_URL in embed.description


def test_build_support_embed_has_footer():
    embed = build_support_embed()
    assert embed.footer.text is not None
    # Names /support, not the retired /games support — the embed is reachable
    # from the top-level command now, and support was never games-specific.
    assert "/support" in embed.footer.text
    assert "/games support" not in embed.footer.text


def test_build_support_embed_uses_golden_meadow_color():
    from bot_modules.games.constants import BRAND_COLOR

    embed = build_support_embed()
    assert embed.color is not None
    assert embed.color.value == BRAND_COLOR


# ── channel-native games: the Survivor pointer and the rooms ──────────


def test_build_help_embed_folds_extra_lines_into_rooms():
    embed = build_help_embed(extra_lines=["🏈 **Survivor** — open in <#5>"])
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["🏠 Rooms & Tables"].startswith("🏈 **Survivor** — open in <#5>")
    assert "Survivor" not in "\n".join(
        f.value or "" for f in build_help_embed().fields
    )


# ── channel-native games: the Survivor pointer ───────────────────────


_GID = 100
_NOW = 1_800_000_000.0


@pytest.fixture
def survivor_db(tmp_path):
    from tests.db_template import migrated_db

    db_path = tmp_path / "help.db"
    migrated_db(db_path)
    return db_path


def _season(conn, **config):
    from bot_modules.services.survivor_service import create_season

    return create_season(conn, _GID, "S", 2026, overrides=config or None)


def _elapse_week_one(conn) -> None:
    from datetime import datetime, timezone

    kicked = datetime.fromtimestamp(_NOW - 86400 * 3, timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
        " kickoff_utc, status, winner) VALUES (2026, 1, 'g1', 'SEA', 'NE', ?,"
        " 'final', 'SEA')",
        (kicked,),
    )


def test_survivor_help_line_is_absent_without_a_season(survivor_db):
    from bot_modules.core.db_utils import open_db

    with open_db(survivor_db) as conn:
        assert survivor_help_line(conn, _GID, _NOW) is None


def test_survivor_help_line_needs_a_wired_channel(survivor_db):
    from bot_modules.core.db_utils import open_db

    with open_db(survivor_db) as conn:
        _season(conn)
        assert survivor_help_line(conn, _GID, _NOW) is None


@pytest.mark.parametrize(
    ("late_entry", "elapsed", "shown"),
    [
        pytest.param("gauntlet", False, True, id="enrolling"),
        pytest.param("gauntlet", True, True, id="gauntlet-door-stays-open"),
        pytest.param("ghost_only", True, True, id="ghost-only-door-stays-open"),
        pytest.param("closed", False, True, id="closed-before-kickoff"),
        pytest.param("closed", True, False, id="closed-after-kickoff"),
    ],
)
def test_survivor_help_line_follows_the_door(survivor_db, late_entry, elapsed, shown):
    from bot_modules.core.db_utils import open_db

    with open_db(survivor_db) as conn:
        _season(conn, channel_id=5551, late_entry=late_entry)
        if elapsed:
            _elapse_week_one(conn)
        line = survivor_help_line(conn, _GID, _NOW)
    if shown:
        assert line is not None and "<#5551>" in line and "Survivor" in line
    else:
        assert line is None


def test_room_help_lines_need_a_wired_channel(survivor_db):
    """Guess Who and the casino get a pointer only once a channel is
    configured — a closed room gets no dead link."""
    from bot_modules.core.db_utils import open_db
    from bot_modules.core.db_utils import set_config_value

    with open_db(survivor_db) as conn:
        assert room_help_lines(conn, _GID) == []
        set_config_value(conn, "guess_channel_id", "777", _GID)
        set_config_value(conn, "casino_channel_id", "888", _GID)
        conn.commit()
        lines = room_help_lines(conn, _GID)
    assert any("Guess Who" in line and "<#777>" in line for line in lines)
    assert any("Casino" in line and "<#888>" in line for line in lines)
