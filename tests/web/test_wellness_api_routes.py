"""Tests for /api/wellness/* — the wellness JSON API.

Most endpoints require the user to have opted in. We seed with
``opt_in_user`` directly so we can exercise the full read/write surface.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_service import (
    add_cap,
    add_blackout,
    create_partner_request,
    opt_in_user,
)


def _opt_in(fake_ctx, user_id: int = 1, *, timezone: str = "UTC"):
    with open_db(fake_ctx.db_path) as conn:
        return opt_in_user(conn, fake_ctx.guild_id, user_id, timezone=timezone)


# ── /me ──────────────────────────────────────────────────────────────


def test_me_returns_not_opted_in_when_no_row(authed_client):
    body = authed_client.get("/api/wellness/me").json()
    assert body == {"opted_in": False}


def test_me_returns_full_summary_when_opted_in(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    body = authed_client.get("/api/wellness/me").json()
    assert body["opted_in"] is True
    assert body["timezone"] == "UTC"
    assert "streak" in body
    assert body["streak"]["current_days"] >= 0
    assert "enforcement_levels" in body
    assert "notification_prefs" in body


# ── /caps ────────────────────────────────────────────────────────────


def test_caps_returns_constants_even_when_empty(authed_client):
    body = authed_client.get("/api/wellness/caps").json()
    assert body["caps"] == []
    assert "global" in body["scopes"]
    assert "daily" in body["windows"]


def test_caps_returns_seeded_caps(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        add_cap(
            conn,
            fake_ctx.guild_id,
            user_id=1,
            label="My limit",
            scope="global",
            scope_target_id=0,
            window="daily",
            cap_limit=10,
        )

    body = authed_client.get("/api/wellness/caps").json()
    assert len(body["caps"]) == 1
    assert body["caps"][0]["label"] == "My limit"


# ── /xp-histogram ────────────────────────────────────────────────────


def test_xp_histogram_rejects_invalid_mode(authed_client):
    resp = authed_client.get("/api/wellness/xp-histogram?mode=bogus")
    body = resp.json()
    assert body["ok"] is False


def test_xp_histogram_daily_returns_24_buckets(authed_client):
    body = authed_client.get("/api/wellness/xp-histogram").json()
    assert body["mode"] == "daily"
    assert len(body["buckets"]) == 24


def test_xp_histogram_weekly_returns_7_buckets(authed_client):
    body = authed_client.get("/api/wellness/xp-histogram?mode=weekly").json()
    assert body["mode"] == "weekly"
    assert len(body["buckets"]) == 7


def test_xp_histogram_clamps_days_into_range(authed_client):
    too_low = authed_client.get("/api/wellness/xp-histogram?days=1").json()
    assert too_low["days_covered"] == 7  # clamped to minimum
    too_high = authed_client.get("/api/wellness/xp-histogram?days=999").json()
    assert too_high["days_covered"] == 180  # clamped to max


# ── /blackouts ───────────────────────────────────────────────────────


def test_get_blackouts_includes_templates_even_when_empty(authed_client):
    body = authed_client.get("/api/wellness/blackouts").json()
    assert body["blackouts"] == []
    assert isinstance(body["templates"], list)


def test_get_blackouts_returns_seeded_rows(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        add_blackout(
            conn,
            fake_ctx.guild_id,
            user_id=1,
            name="Work hours",
            start_minute=9 * 60,
            end_minute=17 * 60,
            days_mask=0b0011111,
        )

    body = authed_client.get("/api/wellness/blackouts").json()
    assert len(body["blackouts"]) == 1
    b = body["blackouts"][0]
    assert b["name"] == "Work hours"
    assert b["start_str"] == "09:00"
    assert b["end_str"] == "17:00"


# ── /away ────────────────────────────────────────────────────────────


def test_away_returns_opted_in_false_for_new_user(authed_client):
    assert authed_client.get("/api/wellness/away").json() == {"opted_in": False}


def test_away_returns_state_when_opted_in(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    body = authed_client.get("/api/wellness/away").json()
    assert body["opted_in"] is True
    assert body["enabled"] is False
    assert "max_len" in body


# ── /partners ────────────────────────────────────────────────────────


def test_partners_empty_for_new_user(authed_client):
    assert authed_client.get("/api/wellness/partners").json() == {"partnerships": []}


def test_partners_resolves_other_user_display_name(authed_client, fake_ctx):
    _opt_in(fake_ctx, user_id=1)
    _opt_in(fake_ctx, user_id=2)
    with open_db(fake_ctx.db_path) as conn:
        create_partner_request(conn, fake_ctx.guild_id, requester_id=1, target_id=2)

    # Attach a guild so display name resolution works
    other = MagicMock()
    other.id = 2
    other.display_name = "Buddy"

    auth_user = MagicMock()
    auth_user.id = 1
    auth_user.bot = False
    auth_user.display_name = "tester"
    auth_user.guild_permissions = MagicMock(value=0x8)
    role = MagicMock(id=0, name="@everyone")
    role.is_default = MagicMock(return_value=True)
    auth_user.roles = [role]

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.members = [auth_user, other]
    guild.get_member = MagicMock(
        side_effect=lambda uid: {1: auth_user, 2: other}.get(int(uid))
    )

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    body = authed_client.get("/api/wellness/partners").json()
    assert len(body["partnerships"]) == 1
    p = body["partnerships"][0]
    assert p["other_id"] == 2
    assert p["other_name"] == "Buddy"
    assert p["is_requester"] is True
    assert p["status"] == "pending"


# ── /history ─────────────────────────────────────────────────────────


def test_history_empty_for_new_user(authed_client):
    assert authed_client.get("/api/wellness/history").json() == {"reports": []}


# ── /settings ────────────────────────────────────────────────────────


def test_settings_rejects_when_not_opted_in(authed_client):
    resp = authed_client.post("/api/wellness/settings", json={"timezone": "UTC"})
    assert resp.status_code == 403


def test_settings_rejects_invalid_enforcement_level(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/settings", json={"enforcement_level": "nope"}
    )
    body = resp.json()
    assert body["ok"] is False
    assert "enforcement_level" in body["error"]


def test_settings_rejects_invalid_notifications_pref(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    body = authed_client.post(
        "/api/wellness/settings", json={"notifications_pref": "bogus"}
    ).json()
    assert body["ok"] is False


@pytest.mark.parametrize("value", ["-1", "24", "abc"])
def test_settings_rejects_invalid_daily_reset_hour(authed_client, fake_ctx, value):
    _opt_in(fake_ctx)
    body = authed_client.post(
        "/api/wellness/settings", json={"daily_reset_hour": value}
    ).json()
    assert body["ok"] is False


def test_settings_accepts_valid_payload(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    body = authed_client.post(
        "/api/wellness/settings",
        json={
            "timezone": "America/New_York",
            "enforcement_level": "cooldown",
            "notifications_pref": "dm",
            "daily_reset_hour": 4,
            "slow_mode_rate_seconds": 30,
        },
    ).json()
    assert body["ok"] is True

    me = authed_client.get("/api/wellness/me").json()
    assert me["timezone"] == "America/New_York"
    assert me["enforcement_level"] == "cooldown"


# ── /pause and /resume ───────────────────────────────────────────────


def test_pause_rejects_invalid_minutes(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    body = authed_client.post("/api/wellness/pause", json={"minutes": 0}).json()
    assert body["ok"] is False
    body = authed_client.post(
        "/api/wellness/pause", json={"minutes": 99_999}
    ).json()
    assert body["ok"] is False


def test_pause_returns_until_timestamp(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    before = time.time()
    body = authed_client.post("/api/wellness/pause", json={"minutes": 30}).json()
    assert body["ok"] is True
    assert body["paused_until"] >= before + 30 * 60 - 5


def test_resume_clears_pause(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    authed_client.post("/api/wellness/pause", json={"minutes": 30})
    body = authed_client.post("/api/wellness/resume").json()
    assert body["ok"] is True

    me = authed_client.get("/api/wellness/me").json()
    assert me["paused_until"] is None


# ── /caps mutation ───────────────────────────────────────────────────


def test_create_cap_rejects_when_not_opted_in(authed_client):
    resp = authed_client.post(
        "/api/wellness/caps",
        json={
            "label": "x",
            "scope": "global",
            "scope_target_id": 0,
            "window": "daily",
            "limit": 1,
        },
    )
    assert resp.status_code == 403


def test_create_cap_requires_label(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/caps",
        json={
            "label": "   ",
            "scope": "global",
            "scope_target_id": 0,
            "window": "daily",
            "limit": 1,
        },
    )
    body = resp.json()
    assert body["ok"] is False


def test_create_cap_persists(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/caps",
        json={
            "label": "Daily limit",
            "scope": "global",
            "scope_target_id": 0,
            "window": "daily",
            "limit": 5,
        },
    )
    body = resp.json()
    assert body["ok"] is True

    listed = authed_client.get("/api/wellness/caps").json()
    assert len(listed["caps"]) == 1
    assert listed["caps"][0]["limit"] == 5


def test_delete_cap_removes_row(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    created = authed_client.post(
        "/api/wellness/caps",
        json={
            "label": "x",
            "scope": "global",
            "scope_target_id": 0,
            "window": "daily",
            "limit": 1,
        },
    ).json()
    cap_id = created["id"]

    resp = authed_client.delete(f"/api/wellness/caps/{cap_id}")
    assert resp.json()["ok"] is True

    listed = authed_client.get("/api/wellness/caps").json()
    assert listed["caps"] == []


def test_delete_cap_returns_404_for_unknown(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    resp = authed_client.delete("/api/wellness/caps/9999")
    assert resp.status_code == 404


# ── /away mutation ───────────────────────────────────────────────────


def test_update_away_rejects_when_not_opted_in(authed_client):
    resp = authed_client.post(
        "/api/wellness/away", json={"enabled": True, "message": "x"}
    )
    assert resp.status_code == 403


def test_update_away_persists_message(authed_client, fake_ctx):
    _opt_in(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/away",
        json={"enabled": True, "message": "Touch grass break"},
    )
    assert resp.json()["ok"] is True

    body = authed_client.get("/api/wellness/away").json()
    assert body["enabled"] is True
    assert body["message"] == "Touch grass break"


# ── Auth gate ─────────────────────────────────────────────────────────


def test_wellness_routes_require_auth(fake_ctx):
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app, raise_server_exceptions=False)
    for path in (
        "/api/wellness/me",
        "/api/wellness/caps",
        "/api/wellness/blackouts",
    ):
        resp = client.get(path)
        assert resp.status_code in (401, 403), f"{path} should require auth"
    client.close()
