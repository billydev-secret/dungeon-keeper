"""Casino view construction — the pieces testable without a gateway.

The hub-view filter is the enforcement of the dashboard's promise that
"unchecked games disappear from the hub panel"; the derby button template
is the bounds guard for stale DynamicItems.
"""

from __future__ import annotations

from bot_modules.cogs.casino.views import DerbyBetButton, build_hub_view
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
