"""Every economy dial with a live reader is reachable from the dashboard.

CLAUDE.md's rule cuts both ways: a preference that isn't enforced must not
ship, and a setting the bot *does* enforce must be settable from the web — not
from a slash command, and not from a hand-written row in the config table.
The 2026-08 config audit found four of each on this feature:

  * the four live-auction guard-rails (min bid, minimum raise, anti-snipe
    window, longest auction) — read on every bid, editable nowhere;
  * ``shop_item_expire_days``, swept hourly, while its four sibling review
    windows all had inputs;
  * a custom item's availability window, staff description and display order —
    accepted by the API, enforced by the shop, absent from the row editor;
  * a catalog icon's display order, which decides which 24 icons the shop
    picker can even show.

And the reverse: ``price_text_room`` / ``price_voice_room`` (a private-rooms
stage nobody built) and ``quest_board_monthly`` (monthly became one guild-wide
goal) were still in the whitelist, priced and sized nothing, and are gone.

These are source-level assertions because the failure mode is silent: a key
that drifts out of one layer still renders, still saves, and still does
nothing. ``encoding="utf-8"`` on every read — the gate's Windows runner
defaults to cp1252 and dies on the em-dashes in these files.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from bot_modules.services.economy_service import EconSettings
from web_server.routes.economy import EconomyConfigUpdate

REPO = Path(__file__).resolve().parents[2]
PRICING = REPO / "src/web_server/static/js/panels/pricing.js"
SINKS = REPO / "src/web_server/static/js/panels/economy-sinks.js"
STATS = REPO / "src/web_server/static/js/panels/economy-stats.js"
QUESTS = REPO / "src/web_server/static/js/panels/economy-quests.js"

DROPPED = ("price_text_room", "price_voice_room", "quest_board_monthly")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pricing_keys() -> set[str]:
    """The settings keys the Pricing page renders an input for."""
    return set(re.findall(r'\[\s*"([a-z][a-z0-9_]+)",\s*"', _text(PRICING)))


def test_the_pricing_page_only_offers_real_settings():
    names = {f.name for f in fields(EconSettings)}
    assert _pricing_keys() <= names


def test_every_pricing_input_can_actually_be_saved():
    """An input whose key is not on the PUT model 422s the whole form."""
    assert _pricing_keys() <= set(EconomyConfigUpdate.model_fields)


@pytest.mark.parametrize(
    "key",
    [
        "auction_min_bid",
        "auction_min_increment",
        "auction_soft_close_seconds",
        "auction_max_duration_hours",
        "shop_item_expire_days",
    ],
)
def test_the_enforced_dial_has_a_pricing_input(key):
    assert key in _pricing_keys()


@pytest.mark.parametrize("key", DROPPED)
def test_a_dropped_dial_is_gone_from_every_layer(key):
    assert key not in {f.name for f in fields(EconSettings)}
    assert key not in EconomyConfigUpdate.model_fields
    for panel in (PRICING, SINKS, STATS, QUESTS):
        assert key not in _text(panel), panel.name


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("data-description", id="staff-description"),
        pytest.param("data-from", id="on-sale-from"),
        pytest.param("data-until", id="on-sale-until"),
        pytest.param("data-sort", id="display-order"),
    ],
)
def test_the_item_editor_renders_the_missing_inputs(marker):
    assert marker in _text(SINKS)


@pytest.mark.parametrize(
    "key",
    ["description", "available_from", "available_until", "sort_order"],
)
def test_the_item_editor_sends_what_it_renders(key):
    """An input nobody puts in the PATCH body is a control that saves nothing."""
    assert re.search(rf"^\s*{key}: ", _text(SINKS), re.M), key


def test_the_icon_row_sends_its_display_order():
    body = _text(SINKS)
    assert "sort_order: sortOrder" in body
