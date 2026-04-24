"""Tests for /api/config/* endpoints."""

from __future__ import annotations

from db_utils import open_db


# ── GET /api/config ───────────────────────────────────────────────────


def test_get_config_returns_expected_sections(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    for section in ("global", "welcome", "xp", "prune", "spoiler", "moderation", "roles", "booster_roles", "auto_delete"):
        assert section in data, f"missing section: {section}"


def test_get_config_requires_auth(fake_ctx):
    from web.auth import DiscordOAuthAuth
    from web.server import create_app
    from fastapi.testclient import TestClient
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/config")
    assert resp.status_code in (401, 403)
    client.close()


# ── PUT /api/config/global ────────────────────────────────────────────


def test_update_global_tz_offset(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/global", json={"tz_offset_hours": -5.0})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "tz_offset_hours", "0", fake_ctx.guild_id)
    assert float(val) == -5.0


def test_update_global_mod_channel(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/global", json={"mod_channel_id": "9999"})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "mod_channel_id", "0", fake_ctx.guild_id)
    assert val == "9999"


def test_update_global_bypass_roles(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/global", json={"bypass_role_ids": ["111", "222"]})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_id_set
        ids = get_config_id_set(conn, "bypass_role_ids", fake_ctx.guild_id)
    assert ids == {111, 222}


def test_update_global_requires_auth(fake_ctx):
    from web.auth import DiscordOAuthAuth
    from web.server import create_app
    from fastapi.testclient import TestClient
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.put("/api/config/global", json={"tz_offset_hours": 0.0})
    assert resp.status_code in (401, 403)
    client.close()


# ── PUT /api/config/welcome ───────────────────────────────────────────


def test_update_welcome_message(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/welcome", json={"welcome_message": "Hello {name}!"})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "welcome_message", "", fake_ctx.guild_id)
    assert val == "Hello {name}!"


def test_update_welcome_channel(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/welcome", json={"welcome_channel_id": "5001"})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        val = get_config_value(conn, "welcome_channel_id", "0", fake_ctx.guild_id)
    assert val == "5001"


# ── PUT /api/config/xp ────────────────────────────────────────────────


def test_update_xp_role_ids(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/xp", json={
        "level_5_role_id": "3001",
        "level_up_log_channel_id": "4001",
    })
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        assert get_config_value(conn, "xp_level_5_role_id", "0", fake_ctx.guild_id) == "3001"
        assert get_config_value(conn, "xp_level_up_log_channel_id", "0", fake_ctx.guild_id) == "4001"


def test_update_xp_excluded_channels(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/xp", json={"xp_excluded_channel_ids": ["7001", "7002"]})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_id_set
        ids = get_config_id_set(conn, "xp_excluded_channel_ids", fake_ctx.guild_id)
    assert ids == {7001, 7002}


def test_update_xp_coefficient(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/xp", json={"message_word_xp": 0.75})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        from xp_system import _XP_COEFF_PREFIX
        val = get_config_value(conn, f"{_XP_COEFF_PREFIX}message_word_xp", "0", fake_ctx.guild_id)
    assert float(val) == 0.75


def test_xp_triggers_reload(authed_client, fake_ctx):
    before = fake_ctx._xp_reload_count
    authed_client.put("/api/config/xp", json={"message_word_xp": 1.0})
    assert fake_ctx._xp_reload_count == before + 1


# ── PUT /api/config/prune ─────────────────────────────────────────────


def test_update_prune_rule(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/prune", json={"role_id": "8001", "inactivity_days": 30})
    assert resp.status_code == 200
    from services.inactivity_prune_service import get_prune_rule
    rule = get_prune_rule(fake_ctx.db_path, fake_ctx.guild_id)
    assert rule is not None
    assert rule["role_id"] == 8001
    assert rule["inactivity_days"] == 30


def test_clear_prune_rule(authed_client, fake_ctx):
    authed_client.put("/api/config/prune", json={"role_id": "8001", "inactivity_days": 30})
    resp = authed_client.put("/api/config/prune", json={"role_id": "0", "inactivity_days": 0})
    assert resp.status_code == 200
    from services.inactivity_prune_service import get_prune_rule
    assert get_prune_rule(fake_ctx.db_path, fake_ctx.guild_id) is None


def test_prune_exemption_add_and_remove(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/prune/exemptions/9001")
    assert resp.status_code == 200
    from services.inactivity_prune_service import get_prune_exception_ids
    assert 9001 in get_prune_exception_ids(fake_ctx.db_path, fake_ctx.guild_id)

    resp = authed_client.delete("/api/config/prune/exemptions/9001")
    assert resp.status_code == 200
    assert 9001 not in get_prune_exception_ids(fake_ctx.db_path, fake_ctx.guild_id)


# ── PUT /api/config/moderation ────────────────────────────────────────


def test_update_moderation_fields(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/moderation", json={
        "jailed_role_id": "6001",
        "log_channel_id": "6002",
        "warning_threshold": 5,
    })
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_value
        assert get_config_value(conn, "jailed_role_id", "0", fake_ctx.guild_id) == "6001"
        assert get_config_value(conn, "log_channel_id", "0", fake_ctx.guild_id) == "6002"
        assert get_config_value(conn, "warning_threshold", "3", fake_ctx.guild_id) == "5"


# ── PUT /api/config/roles/{grant_name} ───────────────────────────────


def test_create_and_delete_role_grant(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/roles/vip", json={
        "label": "VIP",
        "role_id": "5555",
        "log_channel_id": "6666",
        "announce_channel_id": "7777",
        "grant_message": "Welcome to VIP!",
    })
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_grant_roles
        roles = get_grant_roles(conn, fake_ctx.guild_id)
    assert "vip" in roles
    assert roles["vip"]["role_id"] == 5555

    resp = authed_client.delete("/api/config/roles/vip")
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_grant_roles
        roles = get_grant_roles(conn, fake_ctx.guild_id)
    assert "vip" not in roles


# ── PUT /api/config/spoiler ───────────────────────────────────────────


def test_update_spoiler_channels(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/spoiler", json={"spoiler_required_channels": ["1001", "1002"]})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from db_utils import get_config_id_set
        ids = get_config_id_set(conn, "spoiler_required_channels", fake_ctx.guild_id)
    assert ids == {1001, 1002}


# ── PUT /api/config/booster-roles/{role_key} ─────────────────────────


def test_booster_role_upsert_and_delete(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/booster-roles/fire", json={
        "label": "Fire",
        "role_id": "2001",
        "image_path": "/img/fire.png",
        "sort_order": 1,
    })
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        from services.booster_roles import get_booster_roles
        roles = get_booster_roles(conn, fake_ctx.guild_id)
    assert "fire" in [r["role_key"] for r in roles]

    resp = authed_client.delete("/api/config/booster-roles/fire")
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from services.booster_roles import get_booster_roles
        roles = get_booster_roles(conn, fake_ctx.guild_id)
    assert "fire" not in [r["role_key"] for r in roles]


# ── PUT /api/config/auto-delete/{channel_id} ─────────────────────────


def test_auto_delete_rule_upsert_and_delete(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/auto-delete/3001", json={
        "max_age_seconds": 86400,
        "interval_seconds": 3600,
    })
    assert resp.status_code == 200

    from services.auto_delete_service import list_auto_delete_rules_for_guild
    rules = list_auto_delete_rules_for_guild(fake_ctx.db_path, fake_ctx.guild_id)
    assert 3001 in [r["channel_id"] for r in rules]

    resp = authed_client.delete("/api/config/auto-delete/3001")
    assert resp.status_code == 200
    rules = list_auto_delete_rules_for_guild(fake_ctx.db_path, fake_ctx.guild_id)
    assert 3001 not in [r["channel_id"] for r in rules]
