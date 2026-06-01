"""Tests for /api/me, /api/meta/*, /api/system/stats, and /api/config/ai/*."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.ai_config import list_prompts


# ── Shared mock helpers ──────────────────────────────────────────────


def _auth_member():
    """A member object matching the authed_client session (uid=1)."""
    m = MagicMock()
    m.id = 1
    m.name = "tester"
    m.display_name = "tester"
    m.bot = False
    m.guild_permissions = MagicMock(value=0x8)
    m.status = "online"
    default_role = MagicMock(id=0, name="@everyone")
    default_role.is_default = MagicMock(return_value=True)
    m.roles = [default_role]
    return m


def _attach_bot(fake_ctx, *, roles=None, members=None, channels=None):
    members = members or []
    roles = roles or []
    channels = channels or []

    # Always include the auth session user so auth doesn't reject the request.
    auth_user = _auth_member()
    all_members = [auth_user, *members]
    by_id = {m.id: m for m in all_members}

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.name = "Test Guild"
    guild.members = all_members
    guild.roles = roles
    guild.channels = channels
    guild.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot
    return guild


# ── /api/me ───────────────────────────────────────────────────────────


def test_me_returns_user_info_without_bot(authed_client):
    resp = authed_client.get("/api/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "1"
    assert body["username"] == "tester"
    assert "admin" in body["perms"]
    assert body["guild_id"] == "123"
    assert body["status"] is None  # no bot → no live status
    assert body["games_editor_role_id"] is None


def test_me_returns_live_status_when_bot_available(authed_client, fake_ctx):
    _attach_bot(fake_ctx)
    resp = authed_client.get("/api/me")
    body = resp.json()
    assert body["status"] == "online"
    assert body["guild_name"] == "Test Guild"


def test_me_returns_games_editor_role_when_set(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO games_editor_role (guild_id, role_id) VALUES (?, ?)",
            (fake_ctx.guild_id, 555),
        )
    resp = authed_client.get("/api/me")
    assert resp.json()["games_editor_role_id"] == "555"


# ── /api/guilds and /api/guilds/{id}/select ───────────────────────────


def test_guilds_lists_session_guilds(authed_client, fake_ctx):
    resp = authed_client.get("/api/guilds")
    body = resp.json()
    assert body["active_guild_id"] == str(fake_ctx.guild_id)
    assert body["primary_guild_id"] == str(fake_ctx.guild_id)
    assert len(body["guilds"]) == 1
    assert body["guilds"][0]["id"] == str(fake_ctx.guild_id)


def test_select_guild_rejects_unauthorized_guild(authed_client, fake_ctx):
    """A guild the user isn't a member of must be rejected by the cookie validator."""
    resp = authed_client.post("/api/guilds/999/select")
    # update_session_guild returns None for a guild not in the cookie's list
    # → 400 Invalid guild selection
    assert resp.status_code == 400


def test_select_guild_rejects_user_not_in_guild(authed_client, fake_ctx):
    """If the bot reports the target guild but the user isn't a member, 403."""
    target_guild = MagicMock()
    target_guild.id = 999
    target_guild.name = "Other Guild"
    target_guild.get_member = MagicMock(return_value=None)  # user not in this guild
    target_guild.members = []
    target_guild.roles = []

    auth_member = _auth_member()
    home_guild = MagicMock()
    home_guild.id = fake_ctx.guild_id
    home_guild.get_member = MagicMock(return_value=auth_member)
    home_guild.members = [auth_member]
    home_guild.roles = []
    home_guild.channels = []

    bot = MagicMock()
    # Return different guilds for different IDs
    bot.get_guild = MagicMock(
        side_effect=lambda gid: target_guild if gid == 999 else home_guild
    )
    fake_ctx.bot = bot

    resp = authed_client.post("/api/guilds/999/select")
    assert resp.status_code == 403


# ── /api/meta/roles ───────────────────────────────────────────────────


def test_meta_roles_returns_db_fallback_without_bot(authed_client, fake_ctx):
    """No live bot → role list derived from role_events history."""
    import time
    with open_db(fake_ctx.db_path) as conn:
        # Grant > revoke nets to 1 → member_count 1; another role nets to 0.
        conn.execute(
            "INSERT INTO role_events (granted_at, guild_id, user_id, role_name, action) VALUES (?, ?, ?, ?, ?)",
            (time.time(), fake_ctx.guild_id, 100, "VIP", "grant"),
        )
        conn.execute(
            "INSERT INTO role_events (granted_at, guild_id, user_id, role_name, action) VALUES (?, ?, ?, ?, ?)",
            (time.time(), fake_ctx.guild_id, 101, "VIP", "grant"),
        )
        conn.execute(
            "INSERT INTO role_events (granted_at, guild_id, user_id, role_name, action) VALUES (?, ?, ?, ?, ?)",
            (time.time(), fake_ctx.guild_id, 102, "Guest", "grant"),
        )
        conn.execute(
            "INSERT INTO role_events (granted_at, guild_id, user_id, role_name, action) VALUES (?, ?, ?, ?, ?)",
            (time.time(), fake_ctx.guild_id, 102, "Guest", "remove"),
        )

    resp = authed_client.get("/api/meta/roles")
    assert resp.status_code == 200
    rows = resp.json()
    by_name = {r["name"]: r for r in rows}
    assert by_name["VIP"]["member_count"] == 2
    # Guest netted to 0; max(0, 0) — still appears
    assert by_name["Guest"]["member_count"] == 0


def test_meta_roles_returns_live_data_when_bot_available(authed_client, fake_ctx):
    """When the bot guild is connected, roles come from the live guild."""
    role = MagicMock()
    role.id = 9001
    role.name = "Mod"
    role.color = MagicMock(value=0xFF00FF)
    role.members = [MagicMock(), MagicMock()]
    role.position = 5
    role.managed = False
    role.is_default = MagicMock(return_value=False)

    _attach_bot(fake_ctx, roles=[role])
    resp = authed_client.get("/api/meta/roles")
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "Mod"
    assert body[0]["color"] == "#ff00ff"
    assert body[0]["member_count"] == 2


# ── /api/meta/members ─────────────────────────────────────────────────


def test_meta_members_falls_back_to_known_users(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO known_users (user_id, guild_id, username, display_name) VALUES (?, ?, ?, ?)",
            (101, fake_ctx.guild_id, "alice#0001", "Alice"),
        )
        conn.execute(
            "INSERT INTO known_users (user_id, guild_id, username, display_name) VALUES (?, ?, ?, ?)",
            (102, fake_ctx.guild_id, "bob#0002", "Bob"),
        )

    resp = authed_client.get("/api/meta/members")
    body = resp.json()
    names = {m["display_name"] for m in body}
    assert names >= {"Alice", "Bob"}


def test_meta_members_marks_left_members_when_bot_available(authed_client, fake_ctx):
    """Members in known_users but NOT in the live guild get left_server=True."""
    current = MagicMock()
    current.id = 101
    current.name = "alice"
    current.display_name = "Alice"
    current.bot = False

    _attach_bot(fake_ctx, members=[current])

    with open_db(fake_ctx.db_path) as conn:
        # 101 is still present; 102 has left
        conn.execute(
            "INSERT INTO known_users (user_id, guild_id, username, display_name) VALUES (?, ?, ?, ?)",
            (102, fake_ctx.guild_id, "bob#0002", "Bob"),
        )

    resp = authed_client.get("/api/meta/members")
    body = resp.json()
    by_id = {m["id"]: m for m in body}
    assert by_id["101"].get("left_server") is not True  # current
    assert by_id["102"]["left_server"] is True  # left


# ── /api/meta/channels ────────────────────────────────────────────────


def test_meta_channels_filters_to_text_only(authed_client, fake_ctx):
    """With no bot, falls back to processed_messages channels (text only)."""
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO processed_messages (message_id, guild_id, channel_id, user_id, created_at, processed_at) VALUES (?, ?, ?, ?, 0, 0)",
            (1, fake_ctx.guild_id, 555, 1),
        )
        conn.execute(
            "INSERT INTO processed_messages (message_id, guild_id, channel_id, user_id, created_at, processed_at) VALUES (?, ?, ?, ?, 0, 0)",
            (2, fake_ctx.guild_id, 666, 1),
        )

    resp = authed_client.get("/api/meta/channels?types=text")
    body = resp.json()
    assert {c["id"] for c in body} == {"555", "666"}


def test_meta_channels_returns_empty_without_text_in_filter(authed_client):
    """No bot + non-text filter → empty (no fallback for voice/category/thread)."""
    resp = authed_client.get("/api/meta/channels?types=voice")
    assert resp.json() == []


# ── /api/system/stats ─────────────────────────────────────────────────


def test_system_stats_returns_expected_shape(authed_client):
    resp = authed_client.get("/api/system/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "cpu_percent" in body
    assert "memory" in body
    assert "disk" in body
    assert "network" in body
    assert "interfaces" in body
    assert body["memory"]["percent"] >= 0
    assert body["disk"]["percent"] >= 0


# ── /api/config/ai ────────────────────────────────────────────────────


def test_get_ai_config_returns_expected_sections(authed_client):
    with patch("bot_modules.services.ollama_client.status",
               return_value={"available": False, "model": None}):
        resp = authed_client.get("/api/config/ai")
    assert resp.status_code == 200
    body = resp.json()
    assert "llm_status" in body
    assert "known_models" in body
    assert "prompts" in body
    # Every known prompt key must appear in the response
    expected_keys = {p.key for p in list_prompts()}
    actual_keys = {p["key"] for p in body["prompts"]}
    assert actual_keys == expected_keys


def test_ai_config_round_trip_via_dashboard(authed_client, fake_ctx):
    """End-to-end regression: dashboard PUT then dashboard GET should reflect
    the change. Before the guild_id fix, PUT wrote to guild_id=N and GET
    read from guild_id=0, so the response always showed the default.
    """
    new_model = "test-model-xyz"
    authed_client.put(
        "/api/config/ai/models",
        json={"mod_model": new_model, "wellness_model": new_model},
    )

    # GET the config back through the dashboard route — the user's view.
    from unittest.mock import patch
    with patch(
        "bot_modules.services.ollama_client.status",
        return_value={"available": False, "model": None},
    ):
        body = authed_client.get("/api/config/ai").json()

    assert body["mod_model"] == new_model
    assert body["wellness_model"] == new_model


def test_put_ai_models_persists_choice(authed_client, fake_ctx):
    """PUT writes at the active guild_id; GET reads at the same guild_id —
    full round-trip works. This regression-tests the fix for the old
    asymmetry where writes went to the active guild_id but reads always
    used guild_id=0, so dashboard edits silently disappeared."""
    resp = authed_client.put(
        "/api/config/ai/models",
        json={"mod_model": "qwen-7b", "wellness_model": "qwen-7b"},
    )
    assert resp.status_code == 200

    from bot_modules.services.ai_config import get_mod_model, get_wellness_model
    with open_db(fake_ctx.db_path) as conn:
        assert get_mod_model(conn, fake_ctx.guild_id) == "qwen-7b"
        assert get_wellness_model(conn, fake_ctx.guild_id) == "qwen-7b"


def test_put_ai_prompt_persists_override(authed_client, fake_ctx):
    """Full round-trip: PUT a prompt override, GET it back via the same
    guild_id."""
    key = list_prompts()[0].key
    resp = authed_client.put(
        f"/api/config/ai/prompts/{key}", json={"text": "Custom prompt text"}
    )
    assert resp.status_code == 200

    from bot_modules.services.ai_config import get_prompt
    with open_db(fake_ctx.db_path) as conn:
        assert get_prompt(conn, key, fake_ctx.guild_id) == "Custom prompt text"


def test_put_ai_prompt_rejects_unknown_key(authed_client):
    resp = authed_client.put(
        "/api/config/ai/prompts/does_not_exist", json={"text": "x"}
    )
    assert resp.status_code == 404


def test_delete_ai_prompt_clears_override(authed_client, fake_ctx):
    key = list_prompts()[0].key

    # Seed an override and verify the same-guild round-trip works.
    authed_client.put(f"/api/config/ai/prompts/{key}", json={"text": "custom"})
    from bot_modules.services.ai_config import get_prompt_with_source
    with open_db(fake_ctx.db_path) as conn:
        text, is_override = get_prompt_with_source(conn, key, fake_ctx.guild_id)
    assert text == "custom"
    assert is_override is True

    resp = authed_client.delete(f"/api/config/ai/prompts/{key}")
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        _, is_override = get_prompt_with_source(conn, key, fake_ctx.guild_id)
    assert is_override is False  # override cleared, reader falls back to default


def test_delete_ai_prompt_unknown_key_returns_404(authed_client):
    resp = authed_client.delete("/api/config/ai/prompts/does_not_exist")
    assert resp.status_code == 404


def test_put_ai_prompt_model_persists(authed_client, fake_ctx):
    # Pick a prompt whose info.model_key is set (only some prompts support
    # per-command model overrides).
    key = next(p.key for p in list_prompts() if p.model_key)

    resp = authed_client.put(
        f"/api/config/ai/prompts/{key}/model", json={"model": "qwen-13b"}
    )
    assert resp.status_code == 200

    from bot_modules.services.ai_config import get_command_model
    with open_db(fake_ctx.db_path) as conn:
        assert get_command_model(conn, key, fake_ctx.guild_id) == "qwen-13b"


def test_test_ai_prompt_returns_503_when_llm_unavailable(authed_client):
    with patch("bot_modules.services.ollama_client.is_available", return_value=False):
        resp = authed_client.post(
            f"/api/config/ai/prompts/{list_prompts()[0].key}/test",
            json={"user_input": "hi"},
        )
    assert resp.status_code == 503


def test_get_model_status_returns_ollama_status(authed_client):
    with patch(
        "bot_modules.services.ollama_client.status",
        return_value={"available": True, "model": "qwen-7b"},
    ):
        resp = authed_client.get("/api/config/ai/model-status")
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "model": "qwen-7b"}


def test_put_model_source_persists_all_three_fields(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/ai/model-source",
        json={
            "model_path": "/path/to/model.gguf",
            "hf_repo": "user/repo",
            "hf_file": "model.gguf",
        },
    )
    assert resp.status_code == 200

    from bot_modules.core.db_utils import get_config_value
    with open_db(fake_ctx.db_path) as conn:
        assert get_config_value(conn, "llm_model_path", "") == "/path/to/model.gguf"
        assert get_config_value(conn, "llm_hf_repo", "") == "user/repo"
        assert get_config_value(conn, "llm_hf_file", "") == "model.gguf"


def test_post_model_reload_returns_400_when_no_source_configured(authed_client):
    with patch("bot_modules.services.ollama_client.is_available", return_value=False):
        resp = authed_client.post("/api/config/ai/model-reload")
    assert resp.status_code == 400


def test_messages_ai_query_returns_503_when_llm_unavailable(authed_client):
    with patch("bot_modules.services.ollama_client.is_available", return_value=False):
        resp = authed_client.post(
            "/api/messages/ai-query", json={"question": "x", "days": 7}
        )
    assert resp.status_code == 503


def test_messages_ai_query_requires_bot_guild(authed_client, fake_ctx):
    """LLM available but no live guild → 503."""
    with patch("bot_modules.services.ollama_client.is_available", return_value=True):
        resp = authed_client.post(
            "/api/messages/ai-query", json={"question": "x", "days": 7}
        )
    assert resp.status_code == 503


# ── Auth gates ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/me", None),
        ("GET", "/api/guilds", None),
        ("GET", "/api/meta/roles", None),
        ("GET", "/api/meta/members", None),
        ("GET", "/api/meta/channels", None),
        ("GET", "/api/config/ai", None),
        ("GET", "/api/system/stats", None),
    ],
)
def test_meta_routes_require_auth(fake_ctx, method, path, body):
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(path) if method == "GET" else client.post(path, json=body or {})
    assert resp.status_code in (401, 403), f"{method} {path} should require auth"
    client.close()
