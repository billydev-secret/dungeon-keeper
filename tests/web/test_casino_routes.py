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
    assert casino["derby_enabled"] is True
    assert casino["round_idle_seconds"] == 600
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
            "round_idle_seconds": 300,
        },
    )
    assert resp.status_code == 200

    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert s.channel_id == 424242424242424242
    assert (s.min_bet, s.max_bet, s.daily_wager_cap) == (10, 0, 0)
    assert s.slots_enabled is False
    assert s.blackjack_enabled is True  # untouched
    assert s.round_idle_seconds == 300

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
            "/api/config/casino", json={"round_idle_seconds": 5}
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


def test_update_casino_derby_knobs_roundtrip_and_bounds(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/casino",
        json={"derby_enabled": False, "round_idle_seconds": 90},
    )
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert (s.derby_enabled, s.round_idle_seconds) == (False, 90)
    casino = authed_client.get("/api/config").json()["casino"]
    assert casino["derby_enabled"] is False
    assert casino["round_idle_seconds"] == 90
    # Capped at 840s: the round lives in an ephemeral message whose webhook
    # token Discord expires at 15 minutes, so a longer TTL would resolve
    # rounds nobody can be shown the result of.
    assert (
        authed_client.put(
            "/api/config/casino", json={"round_idle_seconds": 900}
        ).status_code
        == 422
    )


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


def test_update_casino_broadcast_threshold_roundtrip_and_bounds(
    authed_client, fake_ctx
):
    resp = authed_client.put(
        "/api/config/casino", json={"broadcast_min_payout": 750}
    )
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        assert load_casino_settings(conn, fake_ctx.guild_id).broadcast_min_payout == 750
    resp = authed_client.get("/api/config")
    assert resp.json()["casino"]["broadcast_min_payout"] == 750
    assert authed_client.put(
        "/api/config/casino", json={"broadcast_min_payout": -1}
    ).status_code == 422


def test_update_casino_blackjack_idle_capped_at_webhook_ttl(authed_client):
    """Ephemeral hand messages are editable for 15 minutes only — the
    dashboard must refuse an idle window the auto-stand can't repaint."""
    ok = authed_client.put(
        "/api/config/casino", json={"blackjack_idle_seconds": 840}
    )
    assert ok.status_code == 200
    too_long = authed_client.put(
        "/api/config/casino", json={"blackjack_idle_seconds": 900}
    )
    assert too_long.status_code == 422


def test_config_exposes_pools_defaults(authed_client):
    casino = authed_client.get("/api/config").json()["casino"]
    # Ships off: the market mints nothing, but it is a new game surface and
    # an admin should choose to run it.
    assert casino["pools_enabled"] is False
    # A snowflake, so it travels as a string like channel_id. "0" = put the
    # market in the casino channel.
    assert casino["pools_channel_id"] == "0"
    assert casino["pools_close_hour"] == 18
    assert casino["pools_takeout_pct"] == 5


def test_update_pools_settings_persist(authed_client, fake_ctx):
    fake_ctx.bot = MagicMock()
    resp = authed_client.put(
        "/api/config/casino",
        json={
            "pools_enabled": True,
            "pools_channel_id": "515151515151515151",
            "pools_close_hour": 20,
            "pools_takeout_pct": 3,
        },
    )
    assert resp.status_code == 200
    casino = authed_client.get("/api/config").json()["casino"]
    assert casino["pools_enabled"] is True
    assert casino["pools_channel_id"] == "515151515151515151"
    assert casino["pools_close_hour"] == 20
    assert casino["pools_takeout_pct"] == 3


def test_pools_and_casino_pages_save_past_each_other(authed_client, fake_ctx):
    """Neither dashboard page clobbers the other's fields.

    Since 2026-07-28 Pools has its own page (`config-pools.js`) and the Casino
    page no longer carries the four `pools_*` fields, so each PUTs a body that
    omits the other's keys entirely. `CasinoConfigUpdate` is every-field-optional
    and the handler drops unset keys, which is what makes that safe — if either
    page ever started sending defaults for the fields it doesn't own, it would
    silently reset the other page.
    """
    with fake_ctx.open_db() as conn:
        save_casino_settings(
            conn,
            fake_ctx.guild_id,
            {
                "channel_id": 424242424242424242,
                "min_bet": 10,
                "pools_enabled": True,
                "pools_channel_id": 515151515151515151,
                "pools_takeout_pct": 3,
            },
        )

    # A Casino-page-shaped save: no pools keys.
    assert authed_client.put(
        "/api/config/casino", json={"min_bet": 25, "slots_enabled": False}
    ).status_code == 200
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert s.pools_enabled is True
    assert s.pools_channel_id == 515151515151515151
    assert s.pools_takeout_pct == 3

    # ...and the reverse: a Pools-page-shaped save, no casino keys.
    assert authed_client.put(
        "/api/config/casino", json={"pools_takeout_pct": 9}
    ).status_code == 200
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert s.pools_takeout_pct == 9
    assert s.channel_id == 424242424242424242
    assert s.min_bet == 25
    assert s.slots_enabled is False


def test_update_pools_rejects_an_impossible_close_hour(authed_client):
    for hour in (-1, 24, 99):
        resp = authed_client.put(
            "/api/config/casino", json={"pools_close_hour": hour}
        )
        assert resp.status_code == 422, hour


def test_update_pools_rejects_a_confiscatory_takeout(authed_client):
    """Capped at 50%: the takeout is burned, so a runaway value would just
    delete members' stakes."""
    assert authed_client.put(
        "/api/config/casino", json={"pools_takeout_pct": 80}
    ).status_code == 422


def test_update_pools_rejects_a_garbage_channel(authed_client):
    assert authed_client.put(
        "/api/config/casino", json={"pools_channel_id": "not-a-snowflake"}
    ).status_code == 400


# ── the metric roster (docs/plans/pools-metric-rotation.md) ────────────


def test_config_exposes_the_metric_catalog_and_an_empty_roster(authed_client):
    """The panel renders its checkboxes from the server's catalogue, so a
    metric added in Python gets a checkbox with no JS change."""
    casino = authed_client.get("/api/config").json()["casino"]
    assert casino["pools_metrics"] == ""  # empty = the whole roster
    catalog = casino["pools_metric_catalog"]
    keys = [m["key"] for m in catalog]
    assert "economy_net" in keys and "messages" in keys
    assert all(m["label"] for m in catalog)


def test_update_pools_metrics_roundtrips(authed_client, fake_ctx):
    fake_ctx.bot = MagicMock()
    resp = authed_client.put(
        "/api/config/casino",
        json={"pools_metrics": "messages,posters"},
    )
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert s.pools_metrics == "messages,posters"
    assert authed_client.get("/api/config").json()["casino"][
        "pools_metrics"
    ] == "messages,posters"


def test_update_pools_metrics_rejects_an_unknown_key(authed_client):
    """A typo must not quietly shrink the roster — the guild would silently
    run one metric, or none."""
    resp = authed_client.put(
        "/api/config/casino",
        json={"pools_metrics": "messages,not_a_metric"},
    )
    assert resp.status_code == 400
    assert "not_a_metric" in resp.json()["detail"]


def test_an_empty_roster_means_all_not_none(authed_client, fake_ctx):
    """Unticking every box must not silently stop the market; that is what
    pools_enabled is for."""
    fake_ctx.bot = MagicMock()
    assert authed_client.put(
        "/api/config/casino", json={"pools_metrics": ""}
    ).status_code == 200
    from bot_modules.services import pools_metrics

    with fake_ctx.open_db() as conn:
        stored = load_casino_settings(conn, fake_ctx.guild_id).pools_metrics
    assert pools_metrics.enabled_keys(stored) == pools_metrics.ALL_KEYS


def test_config_exposes_the_mines_toggle(authed_client):
    casino = authed_client.get("/api/config").json()["casino"]
    assert casino["mines_enabled"] is True


def test_update_casino_mines_toggle_roundtrips(authed_client, fake_ctx):
    fake_ctx.bot = MagicMock()
    resp = authed_client.put(
        "/api/config/casino", json={"mines_enabled": False}
    )
    assert resp.status_code == 200
    with fake_ctx.open_db() as conn:
        assert load_casino_settings(conn, fake_ctx.guild_id).mines_enabled is False
    # ...and back, so a closed table is not a one-way door.
    assert authed_client.put(
        "/api/config/casino", json={"mines_enabled": True}
    ).status_code == 200
    with fake_ctx.open_db() as conn:
        assert load_casino_settings(conn, fake_ctx.guild_id).mines_enabled is True


def test_closing_mines_leaves_the_other_tables_open(authed_client, fake_ctx):
    """Every-field-optional means one checkbox must not carry the rest with it."""
    fake_ctx.bot = MagicMock()
    with fake_ctx.open_db() as conn:
        save_casino_settings(conn, fake_ctx.guild_id, {"keno_enabled": False})
    authed_client.put("/api/config/casino", json={"mines_enabled": False})
    with fake_ctx.open_db() as conn:
        s = load_casino_settings(conn, fake_ctx.guild_id)
    assert (s.mines_enabled, s.keno_enabled, s.slots_enabled) == (False, False, True)
