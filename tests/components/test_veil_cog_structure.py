"""Tier 2 component tests: VeilCog structural/import assertions.

These tests verify the cog module is importable, the class exists,
the async setup function is present, and the /veil_status slash command
is registered — without making any network calls or loading Discord state.
"""
from __future__ import annotations

import asyncio
import importlib


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


def test_veil_status_command_registered():
    """/veil_status app_commands.Command must be registered on VeilCog."""
    from discord import app_commands

    mod = importlib.import_module("cogs.veil_cog")
    cog_cls = mod.VeilCog

    # Collect all app_commands.Command objects attached to the cog class
    command_names = [
        item.name
        for item in cog_cls.__cog_app_commands__
        if isinstance(item, app_commands.Command)
    ]
    assert "veil_status" in command_names, (
        f"/veil_status not found in VeilCog app commands; found: {command_names}"
    )
