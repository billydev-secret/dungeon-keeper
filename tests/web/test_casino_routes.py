"""Tests for the Casino config section (GET /config slice + PUT /config/casino)."""

from __future__ import annotations

from unittest.mock import MagicMock

from bot_modules.services.casino_service import (
    load_casino_settings,
    save_casino_settings,
)


def test_config_includes_casino_section_with_string_ids(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    casino = resp.json()["casino"]
    assert casino["channel_id"] == "0"  # snowflake rule: string, and dark
    assert casino["min_bet"] == 5
    assert casino["max_bet"] == 100
    assert casino["daily_wager_cap"] == 500
    assert casino["coinflip_enabled"] is True
    assert casino["roulette_window_seconds"] == 45
    # bot bookkeeping must not leak to the dashboard
    assert "panel_message_id" not in casino
    assert "panel_channel_id" not in casino


def test_update_casino_persists_and_pokes_the_bot(authed_client, fake_ctx):
    fake_ctx.bot = MagicMock()
    resp = authed_client.put(
        "/api/config/casino",
        json={
            "channel_id": "424242424242424242",
            "min_bet": 10,
            "max_bet": 0,
            "daily_wager_cap": 0,
            "slots_enabled": False,
            "roulette_window_seconds": 60,
        },
    )
    assert resp.status_code == 200

    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert s.channel_id == 424242424242424242
    assert (s.min_bet, s.max_bet, s.daily_wager_cap) == (10, 0, 0)
    assert s.slots_enabled is False
    assert s.blackjack_enabled is True  # untouched
    assert s.roulette_window_seconds == 60

    fake_ctx.bot.dispatch.assert_called_once_with(
        "casino_config_change", fake_ctx.guild_id
    )

    # and the section reads back with the id as a string
    casino = authed_client.get("/api/config").json()["casino"]
    assert casino["channel_id"] == "424242424242424242"


def test_update_casino_zero_channel_closes_the_casino(authed_client, fake_ctx):
    with fake_ctx.open_db() as conn:
        save_casino_settings(conn, fake_ctx.guild_id, {"channel_id": 999})
    resp = authed_client.put("/api/config/casino", json={"channel_id": "0"})
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        assert load_casino_settings(conn, fake_ctx.guild_id).channel_id == 0


def test_update_casino_rejects_min_over_max_even_cross_field(authed_client, fake_ctx):
    # both in one payload
    resp = authed_client.put(
        "/api/config/casino", json={"min_bet": 200, "max_bet": 100}
    )
    assert resp.status_code == 400
    # against the STORED max when only min is sent
    with fake_ctx.open_db() as conn:
        save_casino_settings(conn, fake_ctx.guild_id, {"max_bet": 50})
    resp = authed_client.put("/api/config/casino", json={"min_bet": 60})
    assert resp.status_code == 400
    # max_bet 0 = no ceiling, so any min is fine
    resp = authed_client.put(
        "/api/config/casino", json={"min_bet": 60, "max_bet": 0}
    )
    assert resp.status_code == 200


def test_update_casino_rejects_garbage_channel(authed_client):
    resp = authed_client.put(
        "/api/config/casino", json={"channel_id": "the-meadow"}
    )
    assert resp.status_code == 400


def test_update_casino_rejects_unknown_fields(authed_client):
    # extra="forbid" — panel bookkeeping (or typos) can't sneak through
    resp = authed_client.put(
        "/api/config/casino", json={"panel_message_id": 1}
    )
    assert resp.status_code == 422


def test_update_casino_rejects_out_of_range_values(authed_client):
    assert (
        authed_client.put(
            "/api/config/casino", json={"roulette_window_seconds": 5}
        ).status_code
        == 422
    )
    assert (
        authed_client.put("/api/config/casino", json={"min_bet": 0}).status_code
        == 422
    )
    assert (
        authed_client.put(
            "/api/config/casino", json={"blackjack_idle_seconds": 10}
        ).status_code
        == 422
    )


def test_update_casino_treats_explicit_nulls_as_no_change(authed_client, fake_ctx):
    """Fields sent as JSON null must change nothing — not persist "None"
    (booleans would silently parse back False) and not 500 on the
    min/max cross-check."""
    resp = authed_client.put(
        "/api/config/casino",
        json={"slots_enabled": None, "min_bet": None, "channel_id": None},
    )
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert s.slots_enabled is True
    assert s.min_bet == 5
    assert s.channel_id == 0


def test_update_economy_config_pokes_the_casino_panel(authed_client, fake_ctx):
    """Disabling the economy must reach the casino cog so the hub panel is
    torn down without a restart."""
    from unittest.mock import MagicMock

    fake_ctx.bot = MagicMock()
    resp = authed_client.put("/api/economy/config", json={"enabled": False})
    assert resp.status_code == 200
    fake_ctx.bot.dispatch.assert_called_once_with(
        "casino_config_change", fake_ctx.guild_id
    )


def test_update_economy_config_treats_explicit_nulls_as_no_change(
    authed_client, fake_ctx
):
    resp = authed_client.put("/api/economy/config", json={"currency_name": None})
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE guild_id = ? AND key = ?",
            (fake_ctx.guild_id, "econ_currency_name"),
        ).fetchone()
    assert row is None  # nothing was written


def test_update_casino_jackpot_knobs_roundtrip_and_bounds(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/casino",
        json={"jackpot_enabled": False, "jackpot_cut_pct": 40, "jackpot_seed": 250},
    )
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert (s.jackpot_enabled, s.jackpot_cut_pct, s.jackpot_seed) == (False, 40, 250)
    casino = authed_client.get("/api/config").json()["casino"]
    assert casino["jackpot_cut_pct"] == 40
    assert (
        authed_client.put(
            "/api/config/casino", json={"jackpot_cut_pct": 101}
        ).status_code
        == 422
    )
