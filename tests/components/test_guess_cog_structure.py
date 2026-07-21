"""Tier 2 component tests: GuessCog structural/import assertions.

These tests verify the cog module is importable, the class exists,
the async setup function is present, and the /guess_status slash command
is registered — without making any network calls or loading Discord state.
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import discord

from bot_modules.cogs.guess_cog import GameView, _game_embed


def test_guess_cog_module_imports_cleanly():
    """Importing cogs.guess_cog must not raise ImportError."""
    importlib.import_module("bot_modules.cogs.guess_cog")


def test_guess_cog_class_exists():
    """GuessCog class must be defined in cogs.guess_cog."""
    mod = importlib.import_module("bot_modules.cogs.guess_cog")
    assert hasattr(mod, "GuessCog"), "GuessCog not found in cogs.guess_cog"


def test_setup_function_exists():
    """Module-level setup must be an async (coroutine) function."""
    mod = importlib.import_module("bot_modules.cogs.guess_cog")
    assert hasattr(mod, "setup"), "setup() not found in cogs.guess_cog"
    assert asyncio.iscoroutinefunction(mod.setup), "setup() must be a coroutine function"


def test_guess_group_and_submit_registered():
    """`guess` app_commands.Group and `submit` subcommand must be on GuessCog."""
    from discord import app_commands

    mod = importlib.import_module("bot_modules.cogs.guess_cog")
    cog_cls = mod.GuessCog

    # There should be a Group named "guess" registered on the cog
    group_names = [
        item.name
        for item in cog_cls.__cog_app_commands__
        if isinstance(item, app_commands.Group)
    ]
    assert "guess" in group_names, (
        f"'guess' Group not found in GuessCog app commands; found: {group_names}"
    )

    # The "guess" group must have a "submit" subcommand
    guess_group = next(
        item for item in cog_cls.__cog_app_commands__ if item.name == "guess"
    )
    sub_names = [cmd.name for cmd in guess_group.commands]
    assert "submit" in sub_names, (
        f"'submit' not found in /guess subcommands; found: {sub_names}"
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
    assert "guess_chip_count:99" in ids
    assert "guess_chip_submitter:99" in ids
    assert "guess_guess:99" in ids


def test_solved_game_view_omits_chips():
    bot = MagicMock()
    view = GameView(bot, round_id=42, solved=True)
    children: list[discord.ui.Button] = view.children  # type: ignore[assignment]
    labels = [c.label for c in children]
    assert labels == ["Guess late"]


def test_game_embed_has_no_anonymous_description():
    embed = _game_embed(42)
    assert embed.description in (None, "")
    assert embed.title == "🎭 Round #42"
