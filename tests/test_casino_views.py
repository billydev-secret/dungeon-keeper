"""Casino view construction — the pieces testable without a gateway.

The hub-view filter is the enforcement of the dashboard's promise that
"unchecked games disappear from the hub panel"; the derby button template
is the bounds guard for stale DynamicItems.
"""

from __future__ import annotations

import discord

from bot_modules.cogs.casino.views import (
    DerbyBetButton,
    RoundResolveButton,
    build_hub_view,
)
from bot_modules.services import casino_logic as logic
from bot_modules.services.casino_service import GAMES, CasinoSettings


def _custom_ids(view) -> set[str]:
    return {getattr(item, "custom_id", "") or "" for item in view.children}


def test_hub_view_drops_disabled_tables():
    view = build_hub_view(
        CasinoSettings(derby_enabled=False, slots_enabled=False)
    )
    ids = _custom_ids(view)
    assert "casino:derby" not in ids and "casino:slots" not in ids
    assert {"casino:coinflip", "casino:blackjack", "casino:roulette"} <= ids
    # non-game buttons always stay
    assert {"casino:stats", "casino:help"} <= ids


def test_hub_view_full_house_by_default():
    ids = _custom_ids(build_hub_view(CasinoSettings()))
    assert {f"casino:{game}" for game in GAMES} <= ids


def test_hub_view_game_rows_are_evenly_sized():
    """Todo #87: a 5-wide row wraps on narrow clients, leaving a short
    "derby line". Three per game row fits every client, so every game row
    renders the same width; the two grey utility buttons keep their own row.
    """
    rows: dict[int, list[str]] = {}
    for item in build_hub_view(CasinoSettings()).children:
        rows.setdefault(item.row, []).append(getattr(item, "label", ""))

    assert [len(rows[r]) for r in sorted(rows)] == [3, 3, 3, 2]
    assert rows[3] == ["My Stats", "How It Works"]


def test_derby_button_template_is_bounds_anchored():
    template = DerbyBetButton.__discord_ui_compiled_template__
    assert template.fullmatch(f"casino_dy:{len(logic.DERBY_FIELD) - 1}:12")
    assert template.fullmatch(f"casino_dy:{len(logic.DERBY_FIELD)}:12") is None


def test_resolve_button_template_matches_exactly_the_five_games():
    template = RoundResolveButton.__discord_ui_compiled_template__
    for game in ("roulette", "derby", "baccarat", "dice", "keno"):
        assert template.fullmatch(f"casino_go:{game}:12"), game
    # A stale id for a game that no longer exists must fail the match
    # rather than reach the cog's lookup.
    assert template.fullmatch("casino_go:pools:12") is None
    assert template.fullmatch("casino_go:blackjack:12") is None


def test_every_dynamic_item_is_registered_at_cog_load():
    """A DynamicItem that is never handed to ``add_dynamic_items`` builds
    and renders perfectly and then silently fails to route on click.

    This is glue, not behaviour, which is exactly why it is worth one
    assertion: RoundResolveButton shipped unregistered and every Spin /
    Race / Deal / Roll / Draw press would have done nothing at all.
    """
    import inspect

    from bot_modules.cogs.casino import cog as casino_cog
    from bot_modules.cogs.casino import views as casino_views

    defined = {
        name
        for name, obj in inspect.getmembers(casino_views, inspect.isclass)
        if issubclass(obj, discord.ui.DynamicItem)
        and obj is not discord.ui.DynamicItem
        and obj.__module__ == casino_views.__name__
    }
    source = inspect.getsource(casino_cog.CasinoCog.cog_load)
    registered = {name for name in defined if name in source}
    assert defined == registered, (
        "casino DynamicItems missing from add_dynamic_items (their buttons "
        f"would render but never route): {sorted(defined - registered)}"
    )
