"""The Risky Rolls panel's two payoff dials (`/api/config/risky`).

Both ship at 0 (off). The chaser reads the config rows fresh each tick, so
the route's job is only to store them within bounds — no in-memory cache to
update, unlike the three older dials tested in test_config_routes.py.
"""

from __future__ import annotations

import pytest

from bot_modules.services.risky_roll.logic import PAYOFF_HOURS_MAX, PayoffDials
from bot_modules.services.risky_roll.store import StateStore


def _dials(fake_ctx) -> dict[int, PayoffDials]:
    return StateStore(fake_ctx.db_path)._load_payoff_dials()


def test_risky_section_ships_both_payoff_dials_off(authed_client):
    r = authed_client.get("/api/config").json()["risky"]
    assert r["chase_hours"] == 0
    assert r["fallback_hours"] == 0


def test_update_risky_persists_the_payoff_dials(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/risky", json={"chase_hours": 4, "fallback_hours": 24}
    )
    assert resp.status_code == 200

    r = authed_client.get("/api/config").json()["risky"]
    assert r["chase_hours"] == 4
    assert r["fallback_hours"] == 24
    # The chaser reads the same rows the panel wrote.
    assert _dials(fake_ctx)[fake_ctx.guild_id] == PayoffDials(chase_hours=4, fallback_hours=24)


def test_update_risky_zero_clears_a_payoff_dial(authed_client, fake_ctx):
    authed_client.put("/api/config/risky", json={"chase_hours": 4, "fallback_hours": 24})
    resp = authed_client.put("/api/config/risky", json={"chase_hours": 0})
    assert resp.status_code == 200

    r = authed_client.get("/api/config").json()["risky"]
    assert r["chase_hours"] == 0
    assert r["fallback_hours"] == 24  # untouched by a partial update
    assert _dials(fake_ctx)[fake_ctx.guild_id] == PayoffDials(chase_hours=0, fallback_hours=24)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"chase_hours": -1}, id="negative chase"),
        pytest.param({"fallback_hours": -1}, id="negative fallback"),
        pytest.param({"chase_hours": PAYOFF_HOURS_MAX + 1}, id="chase over a week"),
        pytest.param({"fallback_hours": PAYOFF_HOURS_MAX + 1}, id="fallback over a week"),
    ],
)
def test_update_risky_rejects_out_of_range_payoff_hours(authed_client, fake_ctx, body):
    resp = authed_client.put("/api/config/risky", json=body)
    assert resp.status_code == 400
    assert fake_ctx.guild_id not in _dials(fake_ctx)
