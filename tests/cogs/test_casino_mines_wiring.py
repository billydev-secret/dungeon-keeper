"""The two bits of Mines glue the service layer cannot catch.

Everything about the ladder, the money and the settle races is pinned at the
logic and service layers (``tests/test_casino_logic.py``,
``tests/test_casino_service.py``). Only two things live purely in the cog:

1. **The webhook handle is refreshed on every reveal.** A Mines ladder can run
   19 presses, and the deal interaction's token dies at ~15 minutes — so the
   blackjack habit of storing the handle once at the deal would leave the idle
   auto-cash holding a dead handle and the player watching a frozen grid while
   their payout landed silently. Each press is its own interaction with its own
   fresh token, so re-storing keeps the handle young. Nothing about the money
   depends on it, which is exactly why it would rot unnoticed without a test.

2. **Every live-hand game in the sweep descriptor resolves to a real method.**
   ``_HandUI.resolver`` names the cog method by string so the descriptor can be
   a module constant; a typo or a renamed method would surface only as a stale
   hand nobody settles, inside the sweep's own exception handler.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot_modules.cogs.casino.cog import _HAND_UIS, CasinoCog


def test_every_live_hand_game_resolves_to_a_real_cog_method():
    assert {ui.key for ui in _HAND_UIS} == {"blackjack", "war", "mines"}
    for ui in _HAND_UIS:
        assert callable(getattr(CasinoCog, ui.resolver, None)), ui.resolver


def test_hand_descriptor_reaches_each_games_handle_map():
    """The descriptor holds a getter rather than the map, because the maps are
    instance state — this pins that each getter finds its own game's map."""
    cog = SimpleNamespace(
        _bj_followups={1: "bj"},
        _war_followups={2: "war"},
        _mines_followups={3: "mines"},
    )
    found = {ui.key: ui.followups(cog) for ui in _HAND_UIS}
    assert found == {
        "blackjack": {1: "bj"},
        "war": {2: "war"},
        "mines": {3: "mines"},
    }


class _Followup:
    """Stands in for interaction.followup — identity is all that matters."""

    def __init__(self, tag: str) -> None:
        self.tag = tag


def _interaction(tag: str, message_id: int = 500):
    return SimpleNamespace(
        followup=_Followup(tag),
        message=SimpleNamespace(id=message_id),
    )


@pytest.mark.parametrize("presses", [1, 5, 19])
def test_the_auto_cash_handle_is_refreshed_on_every_press(presses):
    """Stored at the deal, then replaced by each reveal's fresher token."""
    cog = SimpleNamespace(_mines_followups={})
    remember = CasinoCog._remember_mines_handle
    remember(cog, _interaction("deal"), 42, 500)
    dealt = cog._mines_followups[42]

    stamps = [dealt[2]]
    for i in range(presses):
        remember(cog, _interaction(f"press-{i}"), 42, 500)
        stamps.append(cog._mines_followups[42][2])

    webhook, message_id, stored_at = cog._mines_followups[42]
    assert webhook.tag == f"press-{presses - 1}", "kept the stale deal handle"
    assert webhook is not dealt[0]
    assert message_id == 500
    assert stored_at >= dealt[2]
    # Monotonic: a press can only ever make the handle younger.
    assert stamps == sorted(stamps)
