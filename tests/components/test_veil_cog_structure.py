"""Tier 2 component tests: VeilCog structural/import assertions.

These tests verify the cog module is importable, the class exists,
the async setup function is present, and the /veil_status slash command
is registered — without making any network calls or loading Discord state.
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import discord

from cogs.veil_cog import GameView, _game_embed


def test_veil_cog_module_imports_cleanly():
    """Importing cogs.veil_cog must not raise ImportError."""
    importlib.import_module("cogs.veil_cog")


def test_veil_cog_class_exists():
    """VeilCog class must be defined in cogs.veil_cog."""
    mod = importlib.import_module("cogs.veil_cog")
    assert hasattr(mod, "VeilCog"), "VeilCog not found in cogs.veil_cog"


def test_setup_function_exists():
    """Module-level setup must be an async (coroutine) function."""
    mod = importlib.import_module("cogs.veil_cog")
    assert hasattr(mod, "setup"), "setup() not found in cogs.veil_cog"
    assert asyncio.iscoroutinefunction(mod.setup), "setup() must be a coroutine function"


def test_veil_group_and_submit_registered():
    """`veil` app_commands.Group and `submit` subcommand must be on VeilCog."""
    from discord import app_commands

    mod = importlib.import_module("cogs.veil_cog")
    cog_cls = mod.VeilCog

    # There should be a Group named "veil" registered on the cog
    group_names = [
        item.name
        for item in cog_cls.__cog_app_commands__
        if isinstance(item, app_commands.Group)
    ]
    assert "veil" in group_names, (
        f"'veil' Group not found in VeilCog app commands; found: {group_names}"
    )

    # The "veil" group must have a "submit" subcommand
    veil_group = next(
        item for item in cog_cls.__cog_app_commands__ if item.name == "veil"
    )
    sub_names = [cmd.name for cmd in veil_group.commands]
    assert "submit" in sub_names, (
        f"'submit' not found in /veil subcommands; found: {sub_names}"
    )


def test_unsolved_game_view_has_guess_button_and_two_chips():
    bot = MagicMock()
    view = GameView(bot, round_id=42, guess_count=7)
    children: list[discord.ui.Button] = view.children  # type: ignore[assignment]
    assert len(children) == 3
    labels = [c.label for c in children]
    assert "Guess" in labels
    assert "Guesses: 7" in labels
    assert "Submitted by ▒▒▒▒▒▒▒" in labels
    chip_buttons = [c for c in children if c.label and c.label.startswith(("Guesses:", "Submitted by"))]
    for chip in chip_buttons:
        assert chip.disabled is True
        assert chip.style is discord.ButtonStyle.secondary
        assert chip.row == 1


def test_unsolved_game_view_chip_custom_ids_are_round_scoped():
    bot = MagicMock()
    view = GameView(bot, round_id=99, guess_count=0)
    children: list[discord.ui.Button] = view.children  # type: ignore[assignment]
    ids = {c.custom_id for c in children if c.custom_id}
    assert "veil_chip_count:99" in ids
    assert "veil_chip_submitter:99" in ids
    assert "veil_guess:99" in ids


def test_solved_game_view_omits_chips():
    bot = MagicMock()
    view = GameView(bot, round_id=42, solved=True)
    children: list[discord.ui.Button] = view.children  # type: ignore[assignment]
    labels = [c.label for c in children]
    assert labels == ["Guess late"]


def test_game_embed_has_no_anonymous_description():
    embed = _game_embed(42)
    assert embed.description in (None, "")
    assert embed.title == "Round #42"
