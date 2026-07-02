"""Tests for the extracted Games Help modules.

Covers ``bot_modules/games_help/logic.py`` (command and description
lookups, plus alignment guarantees against the canonical
``GAME_ICONS`` registry) and ``bot_modules/games_help/embeds.py``
(``/games-help`` and ``/games-support`` embeds).
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games_help.embeds import build_help_embed, build_support_embed
from bot_modules.games_help.logic import (
    GAME_COMMANDS,
    GAME_DESCRIPTIONS,
    OTHER_COMMANDS_VALUE,
    SUPPORT_INVITE_URL,
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
    assert "/games support" in embed.footer.text


def test_build_support_embed_uses_golden_meadow_color():
    from bot_modules.games.constants import BRAND_COLOR

    embed = build_support_embed()
    assert embed.color is not None
    assert embed.color.value == BRAND_COLOR
