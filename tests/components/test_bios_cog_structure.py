"""Tier 2 component tests: BiosCog structural/import assertions.

Verifies the cog module is importable, the class exists, the async
setup function is present, the /bio slash command is registered, and
the persistent trigger View carries a stable `custom_id` so it survives
restart re-registration.
"""

from __future__ import annotations

import asyncio
import importlib

import discord
from discord import app_commands

from bot_modules.bios.views import PersistentTriggerView


def test_bios_cog_module_imports_cleanly():
    """Importing cogs.bios_cog must not raise."""
    importlib.import_module("bot_modules.cogs.bios_cog")


def test_bios_cog_class_exists():
    mod = importlib.import_module("bot_modules.cogs.bios_cog")
    assert hasattr(mod, "BiosCog")


def test_setup_function_exists_and_is_async():
    mod = importlib.import_module("bot_modules.cogs.bios_cog")
    assert hasattr(mod, "setup")
    assert asyncio.iscoroutinefunction(mod.setup)


def test_bio_slash_command_registered():
    """The /bio app command must be on BiosCog."""
    mod = importlib.import_module("bot_modules.cogs.bios_cog")
    cog_cls = mod.BiosCog
    command_names = [
        item.name
        for item in cog_cls.__cog_app_commands__
        if isinstance(item, app_commands.Command)
    ]
    assert "bio" in command_names, f"/bio not registered; found: {command_names}"


def test_persistent_trigger_view_has_fixed_custom_id():
    """The persistent trigger button must have a stable custom_id so it
    survives bot restart. The cog re-registers this View on cog_load."""
    view = PersistentTriggerView()
    children: list[discord.ui.Button] = view.children  # type: ignore[assignment]
    assert len(children) == 1
    btn = children[0]
    assert btn.custom_id == "bios_trigger"


def test_persistent_trigger_view_is_persistent():
    """timeout=None is required for persistent views."""
    view = PersistentTriggerView()
    assert view.timeout is None
