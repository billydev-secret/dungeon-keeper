"""Web route tests for Billy-bot — setup suggestions and the config panel.

The ask endpoint itself needs a live bot + Anthropic client, so it's exercised
at the service layer (tests/test_advisor_service.py). What's guarded here is
the glue: the admin gate on suggestions, the shape the widget consumes, and
the two-model config round-trip.
"""

from __future__ import annotations

from bot_modules.core.db_utils import set_config_value
from starlette.testclient import TestClient

from bot_modules.services.branding_service import BrandingConfig, upsert_branding
from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app

CH = "111111111111111111"


def _set(fake_ctx, key, value, guild_id=None):
    with fake_ctx.open_db() as conn:
        set_config_value(conn, key, value, guild_id or fake_ctx.guild_id)


# ── GET /api/help/suggestions ───────────────────────────────────────────────


def test_suggestions_on_a_bare_server_lists_gaps(open_client):
    r = open_client.get("/api/help/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["suggestions"]) == 3  # default limit
    first = body["suggestions"][0]
    assert {"slug", "label", "blurb", "panel", "status", "effort", "missing"} <= set(first)
    assert first["status"] in ("ready_but_off", "partial", "unconfigured")


def test_suggestions_returns_guild_id_as_a_string(open_client, fake_ctx):
    """Snowflake precision — ids never go out as bare JSON numbers."""
    body = open_client.get("/api/help/suggestions").json()
    assert body["guild_id"] == str(fake_ctx.guild_id)
    assert isinstance(body["guild_id"], str)


def test_suggestions_limit_is_honoured_and_clamped(open_client):
    assert len(open_client.get("/api/help/suggestions?limit=1").json()["suggestions"]) == 1
    # Out-of-range values clamp rather than erroring or returning everything.
    big = open_client.get("/api/help/suggestions?limit=999").json()["suggestions"]
    assert len(big) <= 10
    assert len(open_client.get("/api/help/suggestions?limit=0").json()["suggestions"]) >= 1


def test_configured_feature_drops_out_of_suggestions(open_client, fake_ctx):
    _set(fake_ctx, "welcome_channel_id", CH)
    body = open_client.get("/api/help/suggestions?limit=10").json()
    assert "welcome" not in {s["slug"] for s in body["suggestions"]}


def test_ready_but_off_sorts_first(open_client, fake_ctx):
    """The cheapest win leads — everything wired, just switched off."""
    _set(fake_ctx, "qa_channel_id", CH)
    _set(fake_ctx, "qa_enabled", "0")
    body = open_client.get("/api/help/suggestions").json()
    assert body["suggestions"][0]["slug"] == "qa_rewards"
    assert body["suggestions"][0]["status"] == "ready_but_off"
    assert body["suggestions"][0]["effort"] == 0


def test_partial_reports_only_what_is_still_missing(open_client, fake_ctx):
    _set(fake_ctx, "ticket_panel_channel_id", CH)
    body = open_client.get("/api/help/suggestions?limit=10").json()
    tickets = next(s for s in body["suggestions"] if s["slug"] == "tickets")
    assert tickets["status"] == "partial"
    assert [m["key"] for m in tickets["missing"]] == ["ticket_category_id"]


def test_suggestions_rejects_an_unauthenticated_caller(authed_client):
    """Covered by the authz sweep too, but named here since it gates recon."""
    authed_client.cookies.clear()
    assert authed_client.get("/api/help/suggestions").status_code in (401, 403)


# ── GET/PUT /api/config/advisor ─────────────────────────────────────────────


def test_advisor_config_defaults_to_tiered_models(open_client):
    body = open_client.get("/api/config/advisor").json()
    assert body["model"] == "claude-haiku-4-5"
    assert body["staff_model"] == "claude-sonnet-5"
    assert body["server_context"] is False
    assert {m["id"] for m in body["models"]} >= {body["model"], body["staff_model"]}


def test_advisor_config_roundtrip(open_client):
    r = open_client.put(
        "/api/config/advisor",
        json={
            "model": "claude-sonnet-5",
            "staff_model": "claude-opus-4-8",
            "server_context": True,
        },
    )
    assert r.status_code == 200, r.text
    body = open_client.get("/api/config/advisor").json()
    assert body["model"] == "claude-sonnet-5"
    assert body["staff_model"] == "claude-opus-4-8"
    assert body["server_context"] is True


def test_advisor_config_rejects_an_unknown_model_on_either_tier(open_client):
    for payload in (
        {"model": "gpt-9", "staff_model": "claude-sonnet-5", "server_context": False},
        {"model": "claude-haiku-4-5", "staff_model": "nope", "server_context": False},
    ):
        assert open_client.put("/api/config/advisor", json=payload).status_code == 400


def test_advisor_config_requires_both_models(open_client):
    r = open_client.put(
        "/api/config/advisor",
        json={"model": "claude-haiku-4-5", "server_context": False},
    )
    assert r.status_code == 422


# ── /api/help/advisor/name + assistant_name in config (per-guild branding) ──
#
# The assistant's *name* is branding_config now (default "Billy-bot"): the
# member-facing Help panel and the admin config panel both read it from there
# rather than printing a baked-in name.


def test_advisor_name_defaults_to_the_builtin(authed_client):
    resp = authed_client.get("/api/help/advisor/name")
    assert resp.status_code == 200
    assert resp.json()["assistant_name"] == "Billy-bot"


def test_advisor_name_follows_branding(authed_client, fake_ctx):
    upsert_branding(
        fake_ctx.db_path,
        BrandingConfig(guild_id=fake_ctx.guild_id, assistant_name="Sam-bot"),
    )
    assert authed_client.get("/api/help/advisor/name").json()["assistant_name"] == "Sam-bot"


def test_advisor_config_reports_the_name(authed_client, fake_ctx):
    body = authed_client.get("/api/config/advisor").json()
    assert body["assistant_name"] == "Billy-bot"
    assert body["model"]  # unchanged fields still present
    assert body["server_context"] is False

    upsert_branding(
        fake_ctx.db_path,
        BrandingConfig(guild_id=fake_ctx.guild_id, assistant_name="Sam-bot"),
    )
    assert authed_client.get("/api/config/advisor").json()["assistant_name"] == "Sam-bot"


def test_advisor_name_requires_a_session(fake_ctx):
    """Member-facing, but still authenticated — no cookie, no name."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    with TestClient(create_app(fake_ctx, auth=auth), follow_redirects=False) as client:
        assert client.get("/api/help/advisor/name").status_code in (401, 403)
