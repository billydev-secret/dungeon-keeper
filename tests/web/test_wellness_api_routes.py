"""Tests for /api/wellness/* — the wellness JSON API.

Most endpoints require the user to have opted in. We seed with
``opt_in_user`` directly so we can exercise the full read/write surface.
"""

from __future__ import annotations

import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_service import (
    add_cap,
    add_blackout,
    arm_slow_mode,
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


# ── /activity-histogram ──────────────────────────────────────────────


def test_activity_histogram_rejects_invalid_mode(authed_client):
    resp = authed_client.get("/api/wellness/activity-histogram?mode=bogus")
    body = resp.json()
    assert body["ok"] is False


def test_activity_histogram_daily_returns_24_buckets(authed_client):
    body = authed_client.get("/api/wellness/activity-histogram").json()
    assert body["mode"] == "daily"
    assert len(body["buckets"]) == 24


def test_activity_histogram_weekly_returns_7_buckets(authed_client):
    body = authed_client.get("/api/wellness/activity-histogram?mode=weekly").json()
    assert body["mode"] == "weekly"
    assert len(body["buckets"]) == 7


def test_activity_histogram_clamps_days_into_range(authed_client):
    too_low = authed_client.get("/api/wellness/activity-histogram?days=1").json()
    assert too_low["days_covered"] == 7  # clamped to minimum
    too_high = authed_client.get("/api/wellness/activity-histogram?days=999").json()
    assert too_high["days_covered"] == 180  # clamped to max


def test_activity_histogram_counts_messages_not_xp(authed_client, fake_ctx):
    """The caps sliders seed from these averages, and enforcement counts
    messages — so the histogram must count message events (source text/reply,
    one row each), never sum fractional XP amounts, and must ignore
    non-message sources like voice."""
    now = time.time()
    with open_db(fake_ctx.db_path) as conn:
        for i in range(10):
            conn.execute(
                "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at) "
                "VALUES (?, 1, 'text', 0.17, ?)",
                (fake_ctx.guild_id, now - 60 * i),
            )
        conn.execute(
            "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at) "
            "VALUES (?, 1, 'voice', 1.0, ?)",
            (fake_ctx.guild_id, now),
        )

    body = authed_client.get("/api/wellness/activity-histogram?days=7").json()
    # 10 message events land in one or two adjacent hour buckets; the voice
    # event must not be counted anywhere.
    assert body["total_events"] == 10
    assert sum(b["count"] for b in body["buckets"]) == 10
    # Averages are messages/period — with 10 messages over a 7-day window the
    # non-empty buckets must sum to 10/7 ≈ 1.4, not to a fraction of an XP point.
    assert sum(b["avg_messages"] for b in body["buckets"]) == pytest.approx(
        10 / 7, abs=0.2
    )


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
            "enforcement_level": "slow_mode",
            "notifications_pref": "dm",
            "daily_reset_hour": 4,
            "slow_mode_rate_seconds": 30,
        },
    ).json()
    assert body["ok"] is True

    me = authed_client.get("/api/wellness/me").json()
    assert me["timezone"] == "America/New_York"
    assert me["enforcement_level"] == "slow_mode"


def test_settings_rejects_retired_cooldown_level(authed_client, fake_ctx):
    """"cooldown" left the selectable set 2026-07-30 (never-enforced level);
    the API must not accept new selections of it."""
    _opt_in(fake_ctx)
    body = authed_client.post(
        "/api/wellness/settings",
        json={"enforcement_level": "cooldown"},
    ).json()
    assert body["ok"] is False


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


# ── /optout ──────────────────────────────────────────────────────────


def test_optout_requires_opt_in(authed_client):
    resp = authed_client.post("/api/wellness/optout")
    assert resp.status_code == 403


def test_optout_deactivates_and_lifts_slow_mode(authed_client, fake_ctx):
    """The exit must be total: tracking off, slow mode lifted, /me shows out."""
    _opt_in(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        arm_slow_mode(
            conn,
            fake_ctx.guild_id,
            1,
            triggered_by_cap_id=1,
            triggered_window_start=0,
            active_until_ts=time.time() + 3600,
        )

    body = authed_client.post("/api/wellness/optout").json()
    assert body["ok"] is True

    me = authed_client.get("/api/wellness/me").json()
    assert me == {"opted_in": False}
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM wellness_slow_mode WHERE guild_id = ? AND user_id = 1",
            (fake_ctx.guild_id,),
        ).fetchone()
    assert row is None


# ── /api/me wellness nav-gate field ──────────────────────────────────


def test_me_wellness_opted_in_tracks_membership(authed_client, fake_ctx):
    """The Wellness nav section gates on this field — it must follow the
    member's actual opt-in state through the whole join/leave loop."""
    assert authed_client.get("/api/me").json()["wellness_opted_in"] is False
    _opt_in(fake_ctx)
    assert authed_client.get("/api/me").json()["wellness_opted_in"] is True
    authed_client.post("/api/wellness/optout")
    assert authed_client.get("/api/me").json()["wellness_opted_in"] is False


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


@pytest.mark.parametrize("scope", ["channel", "category", "voice"])
def test_create_cap_rejects_scoped_without_target(authed_client, fake_ctx, scope):
    # No channel/category picker ships yet, so these scopes can never carry a
    # target and would silently persist scope_target_id=0 (which enforcement
    # cannot match, and for category matches every uncategorized channel).
    # They must be rejected outright, like voice.
    _opt_in(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/caps",
        json={
            "label": "scoped",
            "scope": scope,
            "window": "daily",
            "limit": 5,
        },
    )
    body = resp.json()
    assert body["ok"] is False

    # And nothing was persisted.
    listed = authed_client.get("/api/wellness/caps").json()
    assert listed["caps"] == []


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
