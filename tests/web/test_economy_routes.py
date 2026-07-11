"""Tests for /api/economy/config endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import load_econ_settings
from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
from web_server.server import create_app


def _non_admin_client(fake_ctx) -> TestClient:
    """A session that is a guild member but holds no admin bit."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth), raise_server_exceptions=False)
    cookie = auth.create_session_cookie(
        user_id=42,
        username="rando",
        access_token="token",
        permission_bits=0,  # no admin / moderator / manage_server
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


# ── GET /api/economy/config ────────────────────────────────────────────


def test_get_economy_config_returns_defaults(authed_client):
    resp = authed_client.get("/api/economy/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["currency_name"] == "Coin"
    assert data["currency_plural"] == "Coins"
    assert data["booster_multiplier"] == 1.5
    assert data["xp_per_coin"] == 15.0
    assert data["bank_channel_id"] == 0
    assert data["price_role_color"] == 50


def test_get_economy_config_requires_admin(fake_ctx):
    client = _non_admin_client(fake_ctx)
    resp = client.get("/api/economy/config")
    assert resp.status_code == 403
    client.close()


# ── PUT /api/economy/config ────────────────────────────────────────────


def test_put_partial_update_roundtrips(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/economy/config",
        json={
            "enabled": True,
            "currency_name": "Gem",
            "currency_plural": "Gems",
            "booster_multiplier": 2.0,
            "bank_channel_id": 555,
            "manager_role_id": 777,
            "reward_qotd": 25,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with open_db(fake_ctx.db_path) as conn:
        cfg = load_econ_settings(conn, fake_ctx.guild_id)
    assert cfg.enabled is True
    assert cfg.currency_name == "Gem"
    assert cfg.currency_plural == "Gems"
    assert cfg.booster_multiplier == 2.0
    assert cfg.bank_channel_id == 555
    assert cfg.manager_role_id == 777
    assert cfg.reward_qotd == 25
    # Untouched fields keep their defaults.
    assert cfg.wallet_name == "Wallet"


def test_put_partial_leaves_other_fields_unset(authed_client, fake_ctx):
    """Only the sent key is written — empty icon URL is settable, and the rest
    of the settings are not persisted."""
    resp = authed_client.put(
        "/api/economy/config", json={"currency_icon_url": ""}
    )
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value

        # currency_name was never written, so no econ_ row exists for it.
        assert get_config_value(
            conn, "econ_currency_name", "SENTINEL", fake_ctx.guild_id
        ) == "SENTINEL"


def test_put_rejects_negative_number(authed_client):
    resp = authed_client.put("/api/economy/config", json={"reward_qotd": -1})
    assert resp.status_code == 422


def test_put_rejects_overlong_string(authed_client):
    resp = authed_client.put(
        "/api/economy/config", json={"currency_name": "x" * 33}
    )
    assert resp.status_code == 422


def test_put_rejects_unknown_field(authed_client):
    resp = authed_client.put(
        "/api/economy/config", json={"nonsense_field": 1}
    )
    assert resp.status_code == 422


def test_put_rejects_booster_multiplier_below_one(authed_client):
    resp = authed_client.put(
        "/api/economy/config", json={"booster_multiplier": 0.5}
    )
    assert resp.status_code == 422


def test_put_requires_admin(fake_ctx):
    client = _non_admin_client(fake_ctx)
    resp = client.put("/api/economy/config", json={"enabled": True})
    assert resp.status_code == 403
    client.close()
