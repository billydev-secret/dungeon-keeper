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

import sqlite3
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


# ── the lock the grid is pressed under ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "kept"),
    [
        pytest.param(True, False, id="settled-press-drops-the-lock"),
        pytest.param(False, True, id="live-press-keeps-it-for-the-owner"),
    ],
)
async def test_the_per_hand_lock_is_dropped_once_the_grid_is_gone(terminal, kept):
    """Otherwise ``_mines_locks`` grows for the life of the process.

    The heaviest source is a restart: the boot sweep settles every live grid,
    but the ephemeral messages survive with 20 live-looking tile buttons, and
    every press on one used to mint a lock nothing ever reclaimed.
    """
    cog = SimpleNamespace(_mines_locks={})

    async def press(interaction, hand_id, act):
        return terminal

    cog._mines_press = press
    await CasinoCog._mines_step(
        cog, SimpleNamespace(guild=object()), 77, lambda *a: None
    )
    assert (77 in cog._mines_locks) is kept


@pytest.mark.asyncio
async def test_the_whole_press_runs_under_the_lock():
    """Settle AND repaint, not just the settle.

    The service claim is what protects the money, but two tiles pressed
    together would otherwise race on the message edit and could repaint the
    board backwards — showing an opened tile as pressable, and Cash Out at a
    rung the player has already climbed past.
    """
    cog = SimpleNamespace(_mines_locks={})
    held: list[bool] = []

    async def press(interaction, hand_id, act):
        held.append(cog._mines_locks[77].locked())
        return True

    cog._mines_press = press
    await CasinoCog._mines_step(
        cog, SimpleNamespace(guild=object()), 77, lambda *a: None
    )
    assert held == [True]


@pytest.mark.asyncio
async def test_a_raced_second_deal_apologizes_instead_of_exploding(monkeypatch):
    """The one-live-grid index is the real guard, so the pre-check can lose a
    race — a double-submitted bet modal must land on the same refusal the
    pre-check gives, not Discord's bare "This interaction failed"."""
    from bot_modules.cogs.casino import cog as cog_module

    def _boom(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(cog_module.svc, "deal_mines_hand", _boom)
    said: list[str] = []

    async def _say(interaction, text):
        said.append(text)

    monkeypatch.setattr(cog_module, "safe_ephemeral", _say)

    cog = SimpleNamespace(
        ctx=SimpleNamespace(open_db=lambda: _NullDB()), _last_bets={},
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1), user=SimpleNamespace(id=2), channel_id=3,
    )
    await CasinoCog.deal_mines(cog, interaction, 3, 20)
    assert said == ["❌ You already have a grid open — finish that one first."]


class _NullDB:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
