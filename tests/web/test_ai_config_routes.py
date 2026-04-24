"""Tests for /api/config/ai/* endpoints."""

from __future__ import annotations

from db_utils import open_db

_PROMPT_KEY = "ai_prompt_review"


# ── GET /api/config/ai ────────────────────────────────────────────────


def test_get_ai_config_shape(authed_client):
    resp = authed_client.get("/api/config/ai")
    assert resp.status_code == 200
    data = resp.json()
    assert "mod_model" in data
    assert "prompts" in data
    assert isinstance(data["prompts"], list)
    assert len(data["prompts"]) > 0
    first = data["prompts"][0]
    assert "key" in first
    assert "text" in first
    assert "is_override" in first


# ── PUT /api/config/ai/models ─────────────────────────────────────────


def test_update_mod_model(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/ai/models", json={"mod_model": "claude-haiku-4-5-20251001"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from services.ai_config import get_mod_model
        assert get_mod_model(conn) == "claude-haiku-4-5-20251001"


def test_update_wellness_model(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/ai/models", json={"wellness_model": "claude-haiku-4-5-20251001"})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from services.ai_config import get_wellness_model
        assert get_wellness_model(conn) == "claude-haiku-4-5-20251001"


# ── PUT /api/config/ai/prompts/{key} ─────────────────────────────────


def test_update_prompt_stores_override(authed_client, fake_ctx):
    resp = authed_client.put(
        f"/api/config/ai/prompts/{_PROMPT_KEY}",
        json={"text": "Custom review prompt text."},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from services.ai_config import get_prompt_with_source
        text, is_override = get_prompt_with_source(conn, _PROMPT_KEY)
    assert text == "Custom review prompt text."
    assert is_override is True


def test_update_prompt_unknown_key_returns_not_ok(authed_client):
    resp = authed_client.put(
        "/api/config/ai/prompts/no_such_key",
        json={"text": "ignored"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ── DELETE /api/config/ai/prompts/{key} ──────────────────────────────


def test_reset_prompt_removes_override(authed_client, fake_ctx):
    authed_client.put(
        f"/api/config/ai/prompts/{_PROMPT_KEY}",
        json={"text": "Override text."},
    )
    resp = authed_client.delete(f"/api/config/ai/prompts/{_PROMPT_KEY}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from services.ai_config import get_prompt_with_source
        _, is_override = get_prompt_with_source(conn, _PROMPT_KEY)
    assert is_override is False
