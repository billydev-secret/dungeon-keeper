"""Tests for the extracted Games Help modules.

Covers ``bot_modules/games_help/logic.py`` (command and description
lookups, plus alignment guarantees against the canonical
``GAME_ICONS`` registry) and ``bot_modules/games_help/embeds.py``
(``/games-help`` and ``/games-support`` embeds).
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import DUEL_GAME_KEYS, GAME_ICONS, GAME_NAMES
from bot_modules.games_help.embeds import build_help_embed, build_support_embed
from bot_modules.games_help.logic import (
    GAME_COMMANDS,
    GAME_DESCRIPTIONS,
    OTHER_COMMANDS_VALUE,
    SUPPORT_INVITE_URL,
    survivor_help_line,
)


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


def test_other_commands_value_references_recap():
    """A sanity check that the static other-commands block hasn't been
    silently truncated."""
    assert "/recap" in OTHER_COMMANDS_VALUE
    assert "/games support" in OTHER_COMMANDS_VALUE


# ── doc-count tripwires ──────────────────────────────────────────────
# The feature map and web manual advertise the party-game count in prose.
# These numbers have drifted twice (16 → 17 → 18); fail loudly when a game is
# added to GAME_ICONS without updating the docs. The prose list used to live
# in README.md and moved to docs/features.md 2026-09-03 when the README became
# a pitch — the tripwire follows the list, not the filename.

_PARTY_GAME_KEYS = [k for k in GAME_ICONS if k not in DUEL_GAME_KEYS]
_DOCS_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


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


# ── build_help_embed ─────────────────────────────────────────────────


def test_build_help_embed_has_title_and_description():
    embed = build_help_embed()
    assert embed.title is not None
    assert "Community Games" in embed.title
    assert embed.description is not None
    assert "/games play" in embed.description.lower()


def test_build_help_embed_lists_every_game():
    """One field per GAME_ICONS entry, plus the Other Commands block."""
    embed = build_help_embed()
    field_names = [f.name for f in embed.fields]
    for key in GAME_ICONS:
        expected_label = f"{GAME_ICONS[key]} {GAME_NAMES.get(key, key)}"
        assert expected_label in field_names, f"missing field for {key}"


def test_build_help_embed_includes_other_commands_section():
    embed = build_help_embed()
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "⚙️ Other Commands" in by_name
    assert "/recap" in by_name["⚙️ Other Commands"]


def test_build_help_embed_renders_command_and_description_inline():
    embed = build_help_embed()
    by_name = {f.name: f.value or "" for f in embed.fields}
    # Pick a known game — FFA — and check the value embeds both the
    # command and description.
    ffa_field = by_name[f"{GAME_ICONS['ffa']} {GAME_NAMES['ffa']}"]
    assert "/games play ffa" in ffa_field
    assert GAME_DESCRIPTIONS["ffa"] in ffa_field


def test_build_help_embed_has_footer():
    embed = build_help_embed()
    assert embed.footer.text is not None
    assert "/games help" in embed.footer.text


def test_build_help_embed_uses_golden_meadow_color():
    from bot_modules.games.constants import BRAND_COLOR

    embed = build_help_embed()
    assert embed.color is not None
    assert embed.color.value == BRAND_COLOR


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


# ── channel-native games: the Survivor pointer ───────────────────────


def test_build_help_embed_stays_under_the_field_ceiling_with_extra_lines():
    """Survivor's line is folded into Other Commands, not a field of its
    own: the registry plus that block already sit at Discord's 25-field
    ceiling, and a 26th field is a 400 from Discord."""
    embed = build_help_embed(extra_lines=["🏈 **Survivor** — open in <#5>"])
    assert len(embed.fields) <= 25
    by_name = {f.name: f.value or "" for f in embed.fields}
    other = by_name["⚙️ Other Commands"]
    assert other.startswith(OTHER_COMMANDS_VALUE)
    assert other.rstrip().endswith("🏈 **Survivor** — open in <#5>")
    assert "Survivor" not in (build_help_embed().fields[-1].value or "")


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
