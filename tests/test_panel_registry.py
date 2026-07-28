"""The channel-panel registry, and that every entry still resolves.

``POST /api/panels/{key}/post`` reaches its cog method by *name*, looked up at
request time. Nothing at import time catches a rename or a deleted method — the
first sign would be a 503 in production when an admin presses Post. The
resolution test below is the compile-time check the dynamic lookup doesn't get.

Added 2026-07-28 with the route that replaced six panel-posting slash commands.
"""
from __future__ import annotations

import importlib

import pytest

from bot_modules.services.panel_registry import (
    PANEL_SPECS,
    get_panel_spec,
    list_panel_specs,
)

# Where each registered cog class lives. Kept here rather than in the registry
# because only tests need to import cogs — the registry stays Discord-free so
# it can be read without a bot.
_COG_MODULES = {
    "EconomyCog": "bot_modules.cogs.economy_cog",
    "VoiceMasterCog": "bot_modules.cogs.voice_master_cog",
    "GuessCog": "bot_modules.cogs.guess_cog",
    "JailCog": "bot_modules.cogs.jail_cog",
}


@pytest.mark.parametrize("spec", PANEL_SPECS, ids=lambda s: s.key)
def test_every_panel_resolves_to_a_real_cog_method(spec):
    """A renamed or deleted method would otherwise only surface as a 503 when
    an admin presses Post."""
    module = importlib.import_module(_COG_MODULES[spec.cog])
    cog_class = getattr(module, spec.cog)
    method = getattr(cog_class, spec.method, None)
    assert method is not None, f"{spec.cog}.{spec.method} is gone"
    assert callable(method)


@pytest.mark.parametrize("spec", PANEL_SPECS, ids=lambda s: s.key)
def test_every_panel_method_is_a_coroutine(spec):
    """The route awaits the result; a sync method would raise at request time."""
    import inspect

    module = importlib.import_module(_COG_MODULES[spec.cog])
    method = getattr(getattr(module, spec.cog), spec.method)
    assert inspect.iscoroutinefunction(method)


def test_panel_keys_are_unique():
    keys = [spec.key for spec in PANEL_SPECS]
    assert len(keys) == len(set(keys))


def test_lookup_returns_the_matching_spec():
    spec = get_panel_spec("economy-guide")
    assert spec is not None
    assert spec.cog == "EconomyCog"
    assert spec.method == "post_guide_panel"


def test_lookup_returns_none_for_an_unknown_key():
    """None rather than KeyError, so the route can answer 404 in its own words."""
    assert get_panel_spec("no-such-panel") is None


def test_every_spec_carries_dashboard_copy():
    """The panel renders label and description directly; a blank one would ship
    an unlabelled button next to a destructive-looking action."""
    for spec in list_panel_specs():
        assert spec.label.strip()
        assert spec.description.strip()


def test_registry_covers_the_commands_it_replaced():
    """Guards against a panel quietly losing its dashboard route — the command
    that used to post it is gone, so the route is the only way left."""
    assert {spec.key for spec in PANEL_SPECS} == {
        "economy-guide",      # /bank post-guide
        "economy-leaderboard",  # /bank post-leaderboard
        "economy-shop",       # /bank post-shop
        "voice-control",      # /voice-admin post-panel
        "guess-prompt",       # /guess prompt
        "ticket-panel",       # /ticket panel
    }
