"""Pools view/wiring assertions.

Deliberately thin. The market's behaviour — the pool split, the line, what
settles when — is tested at the logic and service layers; re-proving it
through Discord mocks is the bloat the suite spent 2026-07 removing. What
is only true at this layer: the buttons are persistent and carry no round
id, and Pools stays off the casino hub because it lives in its own channel.
"""

from __future__ import annotations

from bot_modules.cogs.casino.views import (
    PoolsBetModal,
    PoolsPanelView,
    build_hub_view,
)
from bot_modules.services import casino_service as svc
from bot_modules.services import pools_logic as L
from bot_modules.services.casino_service import CasinoSettings


def _custom_ids(view) -> set[str]:
    return {getattr(item, "custom_id", "") or "" for item in view.children}


def test_panel_view_is_persistent_and_side_keyed():
    """No round id in the custom ids: there is one open market per guild, so
    the handler resolves it at click. That is what lets the panel survive a
    restart and a day roll without re-registering anything."""
    view = PoolsPanelView()
    assert view.timeout is None
    assert _custom_ids(view) == {"casino:pools_over", "casino:pools_under"}


def test_bet_modal_carries_its_side():
    assert PoolsBetModal(L.OVER).side == L.OVER
    assert PoolsBetModal(L.UNDER).side == L.UNDER
    assert "Over" in PoolsBetModal(L.OVER).title


def test_pools_stays_off_the_casino_hub():
    """Pools runs a day-long market in its own channel, so it is not one of
    the hub's instant-play tables."""
    assert "pools" not in svc.GAMES
    assert "casino:pools" not in _custom_ids(build_hub_view(CasinoSettings()))


def test_pools_channel_falls_back_to_the_casino_channel():
    assert svc.pools_channel(CasinoSettings(channel_id=7)) == 7
    assert svc.pools_channel(
        CasinoSettings(channel_id=7, pools_channel_id=9)
    ) == 9
    assert svc.pools_channel(CasinoSettings()) == 0


def test_pools_is_gated_by_its_own_toggle():
    assert svc.game_enabled(CasinoSettings(pools_enabled=True), "pools")
    assert not svc.game_enabled(CasinoSettings(), "pools")
