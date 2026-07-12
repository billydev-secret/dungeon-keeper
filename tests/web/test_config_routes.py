"""Tests for /api/config/* endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services.guess_repo import insert_audit_event
from web_server.auth import SESSION_COOKIE, DiscordOAuthAuth
from web_server.server import create_app


# ── End-to-end multi-guild isolation ───────────────────────────────────
#
# These drive the REAL seam: a non-primary guild edits its own config via the
# web API, and the bot-side reader (real GuildConfig.load from the DB, via
# FakeCtx.guild_config) reflects it — while the home guild stays isolated.

_SECOND_GUILD = 88_888_888


def _second_guild_client(fake_ctx) -> TestClient:
    """A Discord-OAuth client whose active guild is a NON-primary second guild
    (the user is a member/admin of both home and the second guild)."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)  # home = fake_ctx.guild_id
    client = TestClient(create_app(fake_ctx, auth=auth))
    cookie = auth.create_session_cookie(
        user_id=1,
        username="tester",
        access_token="token",
        permission_bits=0x8,
        guild_id=_SECOND_GUILD,
        guilds=[
            {"id": fake_ctx.guild_id, "name": "Home", "icon": None},
            {"id": _SECOND_GUILD, "name": "Second", "icon": None},
        ],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


def test_e2e_welcome_config_is_per_guild_and_isolated(fake_ctx):
    client = _second_guild_client(fake_ctx)

    resp = client.put(
        "/api/config/welcome",
        json={
            "welcome_channel_id": "5551234",
            "welcome_message": "Welcome to the second server!",
        },
    )
    assert resp.status_code == 200  # per-guild, not 403

    # Bot-side read for the second guild reflects the edit (real GuildConfig.load).
    second_cfg = fake_ctx.guild_config(_SECOND_GUILD)
    assert second_cfg.welcome_channel_id == 5551234
    assert second_cfg.welcome_message == "Welcome to the second server!"

    # Home guild untouched — strict no-fallback for non-home means no cross-bleed.
    assert fake_ctx.guild_config(fake_ctx.guild_id).welcome_channel_id == 0


def test_e2e_moderation_roles_per_guild_permission_isolation(fake_ctx):
    client = _second_guild_client(fake_ctx)

    resp = client.put("/api/config/moderation", json={"mod_role_ids": "424242"})
    assert resp.status_code == 200

    second_cfg = fake_ctx.guild_config(_SECOND_GUILD)
    assert second_cfg.mod_role_ids == frozenset({424242})

    member = MagicMock()
    member.roles = [MagicMock(id=424242)]
    # Mod in the second guild...
    assert second_cfg.member_is_mod(member) is True
    # ...but NOT in the home guild (no mod roles configured there).
    assert fake_ctx.guild_config(fake_ctx.guild_id).member_is_mod(member) is False


# ── GET /api/guess/audit ───────────────────────────────────────────────


def test_guess_audit_returns_events_for_active_guild(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        insert_audit_event(
            conn, guild_id=fake_ctx.guild_id, actor_id=42,
            action="submit", round_id=1, details={"difficulty": "hard"},
        )
        insert_audit_event(
            conn, guild_id=fake_ctx.guild_id, actor_id=43,
            action="delete", round_id=1, details={"by_mod": True},
        )

    resp = authed_client.get("/api/guess/audit")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 2
    assert events[0]["action"] == "delete"  # newest first
    assert events[1]["action"] == "submit"
    assert events[0]["actor_id"] == "43"  # IDs serialized as strings


def test_guess_audit_filter_by_action(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        insert_audit_event(
            conn, guild_id=fake_ctx.guild_id, actor_id=1,
            action="submit", round_id=1,
        )
        insert_audit_event(
            conn, guild_id=fake_ctx.guild_id, actor_id=1,
            action="solve", round_id=1,
        )

    resp = authed_client.get("/api/guess/audit?action=solve")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["action"] == "solve"


def test_guess_audit_rejects_invalid_action(authed_client):
    resp = authed_client.get("/api/guess/audit?action=hax")
    assert resp.status_code == 400


# ── GET /api/config ───────────────────────────────────────────────────


def test_get_config_returns_expected_sections(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    for section in ("global", "welcome", "xp", "prune", "spoiler", "moderation", "roles", "booster_roles", "auto_delete"):
        assert section in data, f"missing section: {section}"


def test_get_config_requires_auth(fake_ctx):
    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app
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
        from bot_modules.core.db_utils import get_config_value
        val = get_config_value(conn, "tz_offset_hours", "0", fake_ctx.guild_id)
    assert float(val) == -5.0


def test_update_global_mod_channel(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/global", json={"mod_channel_id": "9999"})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        val = get_config_value(conn, "mod_channel_id", "0", fake_ctx.guild_id)
    assert val == "9999"


def test_update_global_bypass_roles(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/global", json={"bypass_role_ids": ["111", "222"]})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_id_set
        ids = get_config_id_set(conn, "bypass_role_ids", fake_ctx.guild_id)
    assert ids == {111, 222}


def test_update_global_requires_auth(fake_ctx):
    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app
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
        from bot_modules.core.db_utils import get_config_value
        val = get_config_value(conn, "welcome_message", "", fake_ctx.guild_id)
    assert val == "Hello {name}!"


def test_update_welcome_channel(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/welcome", json={"welcome_channel_id": "5001"})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        val = get_config_value(conn, "welcome_channel_id", "0", fake_ctx.guild_id)
    assert val == "5001"


def test_update_welcome_invalidates_guild_config_cache(authed_client, fake_ctx):
    """Prime the per-guild cache, edit welcome via the API, confirm the next
    read reflects the edit (cache was dropped)."""
    primed = fake_ctx.guild_config(fake_ctx.guild_id)
    assert primed.welcome_channel_id == 0

    resp = authed_client.put(
        "/api/config/welcome", json={"welcome_channel_id": "8888"}
    )
    assert resp.status_code == 200

    fresh = fake_ctx.guild_config(fake_ctx.guild_id)
    assert fresh is not primed  # cache entry replaced
    assert fresh.welcome_channel_id == 8888


# ── PUT /api/config/xp ────────────────────────────────────────────────


def test_update_xp_role_ids(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/xp", json={
        "level_5_role_id": "3001",
        "level_up_log_channel_id": "4001",
    })
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "xp_level_5_role_id", "0", fake_ctx.guild_id) == "3001"
        assert get_config_value(conn, "xp_level_up_log_channel_id", "0", fake_ctx.guild_id) == "4001"


def test_update_xp_excluded_channels(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/xp", json={"xp_excluded_channel_ids": ["7001", "7002"]})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_id_set
        ids = get_config_id_set(conn, "xp_excluded_channel_ids", fake_ctx.guild_id)
    assert ids == {7001, 7002}


def test_update_xp_coefficient(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/xp", json={"message_word_xp": 0.75})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        from bot_modules.core.xp_system import _XP_COEFF_PREFIX
        val = get_config_value(conn, f"{_XP_COEFF_PREFIX}message_word_xp", "0", fake_ctx.guild_id)
    assert float(val) == 0.75


def test_reaction_given_xp_coefficient_roundtrips(authed_client):
    # Default surfaces on GET before any write.
    before = authed_client.get("/api/config")
    assert before.status_code == 200
    assert before.json()["xp"]["reaction_given_xp"] == 0.34

    put = authed_client.put("/api/config/xp", json={"reaction_given_xp": 0.5})
    assert put.status_code == 200

    after = authed_client.get("/api/config")
    assert after.status_code == 200
    assert after.json()["xp"]["reaction_given_xp"] == 0.5


# ── PUT /api/config/prune ─────────────────────────────────────────────


def test_update_prune_rule(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/prune", json={"role_id": "8001", "inactivity_days": 30})
    assert resp.status_code == 200
    from bot_modules.services.inactivity_prune_service import get_prune_rule
    rule = get_prune_rule(fake_ctx.db_path, fake_ctx.guild_id)
    assert rule is not None
    assert rule["role_id"] == 8001
    assert rule["inactivity_days"] == 30


def test_clear_prune_rule(authed_client, fake_ctx):
    authed_client.put("/api/config/prune", json={"role_id": "8001", "inactivity_days": 30})
    resp = authed_client.put("/api/config/prune", json={"role_id": "0", "inactivity_days": 0})
    assert resp.status_code == 200
    from bot_modules.services.inactivity_prune_service import get_prune_rule
    assert get_prune_rule(fake_ctx.db_path, fake_ctx.guild_id) is None


def test_prune_exemption_add_and_remove(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/prune/exemptions/9001")
    assert resp.status_code == 200
    from bot_modules.services.inactivity_prune_service import get_prune_exception_ids
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
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "jailed_role_id", "0", fake_ctx.guild_id) == "6001"
        assert get_config_value(conn, "log_channel_id", "0", fake_ctx.guild_id) == "6002"
        assert get_config_value(conn, "warning_threshold", "3", fake_ctx.guild_id) == "5"


def test_update_moderation_invalidates_guild_config_cache(authed_client, fake_ctx):
    """Editing mod_role_ids via the API must drop the cached snapshot so
    subsequent permission checks see the new roles."""
    primed = fake_ctx.guild_config(fake_ctx.guild_id)
    assert primed.mod_role_ids == frozenset()

    resp = authed_client.put(
        "/api/config/moderation",
        json={"mod_role_ids": "100,101", "admin_role_ids": "200"},
    )
    assert resp.status_code == 200

    fresh = fake_ctx.guild_config(fake_ctx.guild_id)
    assert fresh is not primed
    assert fresh.mod_role_ids == frozenset({100, 101})
    assert fresh.admin_role_ids == frozenset({200})


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
        from bot_modules.core.db_utils import get_grant_roles
        roles = get_grant_roles(conn, fake_ctx.guild_id)
    assert "vip" in roles
    assert roles["vip"]["role_id"] == 5555

    resp = authed_client.delete("/api/config/roles/vip")
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_grant_roles
        roles = get_grant_roles(conn, fake_ctx.guild_id)
    assert "vip" not in roles


# ── PUT /api/config/spoiler ───────────────────────────────────────────


def test_update_spoiler_channels(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/spoiler", json={"spoiler_required_channels": ["1001", "1002"]})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_id_set
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
        from bot_modules.services.booster_roles import get_booster_roles
        roles = get_booster_roles(conn, fake_ctx.guild_id)
    assert "fire" in [r["role_key"] for r in roles]

    resp = authed_client.delete("/api/config/booster-roles/fire")
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.services.booster_roles import get_booster_roles
        roles = get_booster_roles(conn, fake_ctx.guild_id)
    assert "fire" not in [r["role_key"] for r in roles]


def test_get_config_includes_guess_section(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    v = resp.json()["guess"]
    assert v["channel_id"] == "0"
    assert v["role_id"] == "0"
    assert v["crop_difficulty"] == "medium"
    assert v["guess_cooldown_seconds"] == 60
    assert v["min_image_dimension_px"] == 400
    assert v["max_image_size_mb"] == 10


def test_get_config_exposes_booster_panel_channel(authed_client, fake_ctx):
    """Config GET surfaces the most recently posted booster panel channel."""
    from bot_modules.services.booster_roles import replace_booster_panel_refs

    with open_db(fake_ctx.db_path) as conn:
        replace_booster_panel_refs(
            conn, fake_ctx.guild_id, [(7777, 1), (7777, 2), (7777, 3)]
        )
        conn.commit()

    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["booster_panel_channel_id"] == "7777"


def test_post_booster_panel_requires_bot(authed_client):
    """Without a live bot, the repost endpoint returns 503 rather than 500."""
    resp = authed_client.post(
        "/api/config/booster-roles/post-panel",
        json={"channel_id": "12345"},
    )
    assert resp.status_code == 503


# ── PUT /api/config/auto-delete/{channel_id} ─────────────────────────


def test_auto_delete_rule_upsert_and_delete(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/auto-delete/3001", json={
        "max_age_seconds": 86400,
        "interval_seconds": 3600,
    })
    assert resp.status_code == 200

    from bot_modules.services.auto_delete_service import list_auto_delete_rules_for_guild
    rules = list_auto_delete_rules_for_guild(fake_ctx.db_path, fake_ctx.guild_id)
    assert 3001 in [r["channel_id"] for r in rules]

    resp = authed_client.delete("/api/config/auto-delete/3001")
    assert resp.status_code == 200
    rules = list_auto_delete_rules_for_guild(fake_ctx.db_path, fake_ctx.guild_id)
    assert 3001 not in [r["channel_id"] for r in rules]


def test_auto_delete_media_only_round_trips(authed_client, fake_ctx):
    from bot_modules.services.auto_delete_service import list_auto_delete_rules_for_guild

    # media_only defaults to False when omitted.
    authed_client.put("/api/config/auto-delete/3002", json={
        "max_age_seconds": 86400,
        "interval_seconds": 3600,
    })
    rules = {r["channel_id"]: r for r in list_auto_delete_rules_for_guild(
        fake_ctx.db_path, fake_ctx.guild_id
    )}
    assert bool(rules[3002]["media_only"]) is False

    # Setting it persists, and the config payload surfaces it.
    authed_client.put("/api/config/auto-delete/3002", json={
        "max_age_seconds": 86400,
        "interval_seconds": 3600,
        "media_only": True,
    })
    config = authed_client.get("/api/config").json()
    entry = next(e for e in config["auto_delete"] if e["channel_id"] == "3002")
    assert entry["media_only"] is True


# ── PUT /api/config/guess ──────────────────────────────────────────────


def test_update_guess_channel(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/guess", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        val = get_config_value(conn, "guess_channel_id", "0", fake_ctx.guild_id)
    assert val == "555"


def test_update_guess_crop_difficulty_hard(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/guess", json={"crop_difficulty": "hard"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        val = get_config_value(conn, "guess_crop_difficulty", "medium", fake_ctx.guild_id)
    assert val == "hard"


def test_update_guess_invalid_difficulty_returns_error(authed_client):
    resp = authed_client.put("/api/config/guess", json={"crop_difficulty": "insane"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "crop_difficulty" in data["detail"]


# ── Multi-guild safety ───────────────────────────────────────────────
#
# The prelaunch fix removed ``_require_primary_guild`` from every config
# endpoint — the multi-guild migration moved each feature's state into
# per-guild storage, so a session active on guild N can edit guild N's
# config independently from the home guild. The previous version of this
# test asserted 403s that no longer exist by design; the replacement
# below verifies the new contract: non-primary edits succeed AND don't
# corrupt the home guild's flat ctx fields.


def _non_primary_client(fake_ctx, *, other_guild_id: int = 999):
    """Build a TestClient whose session's active guild is NOT the primary."""
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
    from web_server.server import create_app

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    cookie = auth.create_session_cookie(
        user_id=1,
        username="tester",
        access_token="token",
        permission_bits=0x8,  # admin
        guild_id=other_guild_id,
        guilds=[
            {"id": fake_ctx.guild_id, "name": "Home", "icon": None},
            {"id": other_guild_id, "name": "Other", "icon": None},
        ],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


def test_birthday_edit_from_non_primary_guild_lands_at_that_guild(fake_ctx):
    """A non-primary edit writes to the active guild's bucket, not home's."""
    from bot_modules.core.db_utils import get_config_value

    client = _non_primary_client(fake_ctx, other_guild_id=999)
    resp = client.put("/api/config/birthday", json={"birthday_channel_id": "8888"})
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        assert get_config_value(conn, "birthday_channel_id", "0", 999) == "8888"
        # Home guild row untouched.
        assert get_config_value(conn, "birthday_channel_id", "0", fake_ctx.guild_id) == "0"
    client.close()


def test_starboard_edit_from_non_primary_guild_isolates_to_that_guild(fake_ctx):
    """Each guild has its own starboard config — a non-primary edit must
    not bleed into the home guild's starboard."""
    from bot_modules.services.starboard_service import get_starboard_config

    client = _non_primary_client(fake_ctx, other_guild_id=999)
    resp = client.put("/api/config/starboard", json={"threshold": 9})
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        other_cfg = get_starboard_config(conn, 999)
        home_cfg = get_starboard_config(conn, fake_ctx.guild_id)
    assert other_cfg is not None
    assert int(other_cfg["threshold"]) == 9
    # The home guild has no starboard row (nothing was written there).
    assert home_cfg is None
    client.close()


# ── /config/starboard ────────────────────────────────────────────────


def test_update_starboard_persists_fields(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/starboard",
        json={
            "channel_id": "777",
            "threshold": 7,
            "emoji": "🔥",
            "enabled": True,
            "excluded_channels": ["100", "200"],
        },
    )
    assert resp.status_code == 200

    from bot_modules.services.starboard_service import get_starboard_config
    with open_db(fake_ctx.db_path) as conn:
        cfg = get_starboard_config(conn, fake_ctx.guild_id)
    assert cfg is not None
    assert int(cfg["channel_id"]) == 777
    assert int(cfg["threshold"]) == 7
    assert cfg["emoji"] == "🔥"
    assert int(cfg["enabled"]) == 1


def test_update_starboard_rejects_threshold_below_one(authed_client):
    resp = authed_client.put("/api/config/starboard", json={"threshold": 0})
    assert resp.status_code == 400
    assert "Threshold" in resp.json()["detail"]


def test_update_starboard_rejects_empty_emoji(authed_client):
    resp = authed_client.put("/api/config/starboard", json={"emoji": "   "})
    assert resp.status_code == 400


# ── /config/birthday ─────────────────────────────────────────────────


def test_update_birthday_persists_channel_and_message(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/birthday",
        json={"birthday_channel_id": "5050", "birthday_message": "Happy bday {name}!"},
    )
    assert resp.status_code == 200
    from bot_modules.core.db_utils import get_config_value
    with open_db(fake_ctx.db_path) as conn:
        assert get_config_value(conn, "birthday_channel_id", "", fake_ctx.guild_id) == "5050"
        assert get_config_value(conn, "birthday_message", "", fake_ctx.guild_id) == "Happy bday {name}!"


def test_update_birthday_rejects_empty_message(authed_client):
    resp = authed_client.put(
        "/api/config/birthday", json={"birthday_message": "   "}
    )
    assert resp.status_code == 400


# ── /config/risky — in-memory state must be updated ──────────────────


def test_update_risky_persists_and_updates_in_memory_state(authed_client, fake_ctx):
    """The risky_roll in-memory `state.ping_roles` dict must reflect the write."""
    from bot_modules.services.risky_roll import state as rr_state

    rr_state.ping_roles.pop(fake_ctx.guild_id, None)
    rr_state.min_game_seconds.pop(fake_ctx.guild_id, None)

    resp = authed_client.put(
        "/api/config/risky",
        json={"ping_role_id": "5555", "min_game_seconds": 90},
    )
    assert resp.status_code == 200

    assert rr_state.ping_roles[fake_ctx.guild_id] == 5555
    assert rr_state.min_game_seconds[fake_ctx.guild_id] == 90


def test_update_risky_zero_values_clear_in_memory_state(authed_client, fake_ctx):
    """ping_role_id=0 and min_game_seconds=0 mean 'clear' — must drop from state."""
    from bot_modules.services.risky_roll import state as rr_state

    rr_state.ping_roles[fake_ctx.guild_id] = 1234
    rr_state.min_game_seconds[fake_ctx.guild_id] = 60

    resp = authed_client.put(
        "/api/config/risky",
        json={"ping_role_id": "0", "min_game_seconds": 0},
    )
    assert resp.status_code == 200
    assert fake_ctx.guild_id not in rr_state.ping_roles
    assert fake_ctx.guild_id not in rr_state.min_game_seconds


def test_update_risky_rejects_negative_min_seconds(authed_client):
    resp = authed_client.put(
        "/api/config/risky", json={"min_game_seconds": -1}
    )
    assert resp.status_code == 400


# ── /config/policy ───────────────────────────────────────────────────


def test_update_policy_persists_timeout(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/policy", json={"vote_timeout_hours": 72}
    )
    assert resp.status_code == 200
    from bot_modules.core.db_utils import get_config_value
    with open_db(fake_ctx.db_path) as conn:
        val = get_config_value(conn, "policy_vote_timeout_hours", "0", fake_ctx.guild_id)
    assert val == "72"


def test_update_policy_rejects_below_one(authed_client):
    resp = authed_client.put("/api/config/policy", json={"vote_timeout_hours": 0})
    assert resp.status_code == 400


# ── /config/whisper ──────────────────────────────────────────────────


def test_update_whisper_persists_fields(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/whisper",
        json={"channel_id": "7000", "role_id": "8000", "log_channel_id": "9000"},
    )
    assert resp.status_code == 200

    from bot_modules.services.whisper_repo import get_whisper_config
    with open_db(fake_ctx.db_path) as conn:
        cfg = get_whisper_config(conn, fake_ctx.guild_id)
    assert cfg.channel_id == 7000
    assert cfg.role_id == 8000
    assert cfg.log_channel_id == 9000


# ── /config/dms ──────────────────────────────────────────────────────


def test_update_dms_persists_channels(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/dms",
        json={"request_channel_id": "1100", "audit_channel_id": "1200"},
    )
    assert resp.status_code == 200

    from bot_modules.services.dm_perms_service import (
        load_audit_channels,
        load_request_channels,
    )
    assert load_request_channels(fake_ctx.db_path).get(fake_ctx.guild_id) == 1100
    assert load_audit_channels(fake_ctx.db_path).get(fake_ctx.guild_id) == 1200


def test_update_dms_persists_mode_roles(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/dms",
        json={"open_role_id": "500", "ask_role_id": "0", "closed_role_id": "600"},
    )
    assert resp.status_code == 200

    from bot_modules.services.dm_perms_service import get_dm_mode_role_ids
    assert get_dm_mode_role_ids(fake_ctx.db_path, fake_ctx.guild_id) == {
        "open": 500,
        "ask": 0,
        "closed": 600,
    }

    # Partial update: only one field changes, the rest are preserved.
    resp = authed_client.put("/api/config/dms", json={"ask_role_id": "550"})
    assert resp.status_code == 200
    assert get_dm_mode_role_ids(fake_ctx.db_path, fake_ctx.guild_id) == {
        "open": 500,
        "ask": 550,
        "closed": 600,
    }


# ── /config/confessions (PUT + block/unblock) ────────────────────────


def test_update_confessions_creates_config_when_missing(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/confessions",
        json={
            "dest_channel_id": "3000",
            "log_channel_id": "3001",
            "cooldown_seconds": 120,
            "max_chars": 500,
            "replies_enabled": True,
            "per_day_limit": 5,
        },
    )
    assert resp.status_code == 200

    from bot_modules.services.confessions_service import get_config
    cfg = get_config(fake_ctx.db_path, fake_ctx.guild_id)
    assert cfg is not None
    assert cfg.dest_channel_id == 3000
    assert cfg.log_channel_id == 3001
    assert cfg.cooldown_seconds == 120
    assert cfg.replies_enabled is True


def test_confessions_block_user_requires_existing_config(authed_client):
    """Block call must 404 if confessions isn't configured yet."""
    resp = authed_client.put("/api/config/confessions/block/42")
    assert resp.status_code == 404


def test_confessions_block_and_unblock_round_trip(authed_client, fake_ctx):
    # Seed a config so block/unblock have something to mutate
    authed_client.put(
        "/api/config/confessions",
        json={"dest_channel_id": "3000", "log_channel_id": "3001"},
    )

    authed_client.put("/api/config/confessions/block/42")

    from bot_modules.services.confessions_service import get_config
    cfg = get_config(fake_ctx.db_path, fake_ctx.guild_id)
    assert cfg is not None
    assert 42 in cfg.blocked_set()

    authed_client.delete("/api/config/confessions/block/42")
    cfg = get_config(fake_ctx.db_path, fake_ctx.guild_id)
    assert cfg is not None
    assert 42 not in cfg.blocked_set()


# ── /config/confessions/post-button — web→Discord ────────────────────


def test_confessions_post_button_503_when_bot_unavailable(authed_client):
    """No bot attached → endpoint refuses, can't post to Discord."""
    resp = authed_client.post(
        "/api/config/confessions/post-button", json={"channel_id": "1"}
    )
    assert resp.status_code == 503
    assert "Bot" in resp.json()["detail"]


def test_confessions_post_button_503_when_cog_missing(authed_client, fake_ctx):
    """Bot is up but ConfessionsCog isn't loaded — refuse instead of crashing."""
    from unittest.mock import MagicMock

    bot = MagicMock()
    bot.cogs = {}  # no ConfessionsCog
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/confessions/post-button", json={"channel_id": "1"}
    )
    assert resp.status_code == 503
    assert "Confessions" in resp.json()["detail"]


def test_confessions_post_button_invokes_cog_method(authed_client, fake_ctx):
    """Happy path: route forwards to ``cog.web_post_launcher(guild_id, channel_id)``."""
    from unittest.mock import AsyncMock, MagicMock

    cog = MagicMock()
    cog.web_post_launcher = AsyncMock(return_value=True)
    bot = MagicMock()
    bot.cogs = {"ConfessionsCog": cog}
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/confessions/post-button", json={"channel_id": "555"}
    )
    assert resp.status_code == 200
    cog.web_post_launcher.assert_awaited_once_with(fake_ctx.guild_id, 555)


def test_confessions_post_button_500_when_cog_returns_failure(authed_client, fake_ctx):
    """If the cog reports the post failed (e.g. missing channel/perms), surface 500."""
    from unittest.mock import AsyncMock, MagicMock

    cog = MagicMock()
    cog.web_post_launcher = AsyncMock(return_value=False)
    bot = MagicMock()
    bot.cogs = {"ConfessionsCog": cog}
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/confessions/post-button", json={"channel_id": "555"}
    )
    assert resp.status_code == 500


# ── /config/dms/post-panel — web→Discord ─────────────────────────────


def test_dms_post_panel_503_when_bot_unavailable(authed_client):
    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "1"}
    )
    assert resp.status_code == 503


def test_dms_post_panel_503_when_cog_missing(authed_client, fake_ctx):
    from unittest.mock import MagicMock

    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "1"}
    )
    assert resp.status_code == 503


def test_dms_post_panel_503_when_guild_missing(authed_client, fake_ctx):
    from unittest.mock import MagicMock

    cog = MagicMock()
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_guild = MagicMock(return_value=None)
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "1"}
    )
    assert resp.status_code == 503


def _dms_panel_guild(fake_ctx, *, perms=None, channel=None):
    """Build the bot/guild/channel mock scaffolding for post-panel tests."""
    import discord
    from unittest.mock import AsyncMock, MagicMock

    cog = MagicMock()
    cog._ensure_panel = AsyncMock(return_value=88888)
    cog.panel_settings = {}

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    if channel is None:
        channel = MagicMock(spec=discord.TextChannel)
        channel.name = "general"
        channel.permissions_for = MagicMock(
            return_value=perms
            if perms is not None
            else discord.Permissions(
                view_channel=True, send_messages=True, embed_links=True
            )
        )
    guild.get_channel = MagicMock(return_value=channel)

    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot
    return cog, guild


def test_dms_post_panel_invokes_ensure_panel_and_persists_ids(authed_client, fake_ctx):
    """Happy path: cog._ensure_panel(guild, channel_id, force_repost=True) is
    awaited; the returned message_id is persisted via set_panel_settings; and
    the cog's in-memory panel_settings dict is updated."""
    cog, guild = _dms_panel_guild(fake_ctx)

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 200
    cog._ensure_panel.assert_awaited_once_with(guild, 5000, force_repost=True)
    # In-memory cache
    assert cog.panel_settings[fake_ctx.guild_id] == {
        "panel_channel_id": 5000,
        "panel_message_id": 88888,
    }
    # DB row
    from bot_modules.services.dm_perms_service import load_panel_settings
    persisted = load_panel_settings(fake_ctx.db_path).get(fake_ctx.guild_id)
    assert persisted is not None
    assert persisted["panel_channel_id"] == 5000
    assert persisted["panel_message_id"] == 88888


def test_dms_post_panel_400_when_channel_not_text(authed_client, fake_ctx):
    """get_channel returns a non-TextChannel (or None) → 400, no send."""
    from unittest.mock import MagicMock

    cog, guild = _dms_panel_guild(fake_ctx, channel=MagicMock())

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 400
    cog._ensure_panel.assert_not_awaited()


def test_dms_post_panel_400_names_missing_permissions(authed_client, fake_ctx):
    """Bot lacks send/embed in the channel → 400 whose detail names the
    missing permissions, and nothing is posted or persisted."""
    import discord

    cog, guild = _dms_panel_guild(
        fake_ctx, perms=discord.Permissions(view_channel=True)
    )

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Send Messages" in detail
    assert "Embed Links" in detail
    assert "View Channel" not in detail
    cog._ensure_panel.assert_not_awaited()
    assert cog.panel_settings == {}


def test_dms_post_panel_502_when_discord_rejects_send(authed_client, fake_ctx):
    """Perms look fine but the actual send still fails (_ensure_panel → None):
    the route must report an error instead of a false success."""
    cog, guild = _dms_panel_guild(fake_ctx)
    cog._ensure_panel.return_value = None

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 502
    assert cog.panel_settings == {}


# ── /config/booster-roles/post-panel happy + sync-swatches ───────────


def test_booster_post_panel_503_when_guild_missing(authed_client, fake_ctx):
    """Bot is up but doesn't know the guild → can't post to Discord."""
    from unittest.mock import MagicMock

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=None)
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/booster-roles/post-panel", json={"channel_id": "1234"}
    )
    assert resp.status_code == 503


def test_booster_post_panel_400_when_channel_not_text(authed_client, fake_ctx):
    """get_channel returns a non-TextChannel (e.g. voice) → 400, no send."""
    from unittest.mock import MagicMock

    auth_user = MagicMock()
    auth_user.id = 1
    auth_user.bot = False
    auth_user.guild_permissions = MagicMock(value=0x8)
    auth_user.display_name = "tester"
    role = MagicMock(id=0, name="@everyone")
    role.is_default = MagicMock(return_value=True)
    auth_user.roles = [role]

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.get_member = MagicMock(return_value=auth_user)
    # Plain MagicMock isn't a discord.TextChannel
    guild.get_channel = MagicMock(return_value=MagicMock())
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    resp = authed_client.post(
        "/api/config/booster-roles/post-panel", json={"channel_id": "1234"}
    )
    assert resp.status_code == 400


# ── /config/welcome/preview — admin-only render ──────────────────────


def test_welcome_preview_503_when_bot_unavailable(authed_client):
    resp = authed_client.get("/api/config/welcome/preview")
    assert resp.status_code == 503


def test_welcome_preview_renders_with_bot_member(authed_client, fake_ctx):
    """When the bot guild and the auth user are reachable, preview returns
    rendered embed dicts for both welcome and leave."""
    from unittest.mock import MagicMock

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.name = "Test"
    guild.member_count = 5

    auth_user = MagicMock()
    auth_user.id = 1
    auth_user.bot = False
    auth_user.display_name = "tester"
    auth_user.name = "tester"
    auth_user.mention = "<@1>"
    auth_user.guild = guild  # back-ref needed by build_*_embed
    auth_user.guild_permissions = MagicMock(value=0x8)
    auth_user.avatar = None
    role = MagicMock(id=0, name="@everyone")
    role.is_default = MagicMock(return_value=True)
    auth_user.roles = [role]
    auth_user.created_at = MagicMock()
    auth_user.created_at.strftime = MagicMock(return_value="2026-01-01")

    guild.get_member = MagicMock(return_value=auth_user)
    guild.me = auth_user

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    resp = authed_client.get("/api/config/welcome/preview")
    # 200 happy path; the route's build_*_embed helpers are exercised. If the
    # preview shape changes later, this is a useful regression bait.
    assert resp.status_code == 200
    body = resp.json()
    assert "welcome" in body
    assert "leave" in body


# ── PUT /api/config/guess — NSFW channel guard ─────────────────────────


def _attach_guess_bot(fake_ctx, *, nsfw: bool):
    channel = MagicMock()
    channel.is_nsfw = MagicMock(return_value=nsfw)
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=channel)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot


def test_update_guess_channel_rejects_non_nsfw_channel(authed_client, fake_ctx):
    _attach_guess_bot(fake_ctx, nsfw=False)

    resp = authed_client.put("/api/config/guess", json={"channel_id": "555"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "age-gated" in data["detail"]
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "guess_channel_id", "0", fake_ctx.guild_id) == "0"


def test_update_guess_channel_accepts_nsfw_channel(authed_client, fake_ctx):
    _attach_guess_bot(fake_ctx, nsfw=True)

    resp = authed_client.put("/api/config/guess", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "guess_channel_id", "0", fake_ctx.guild_id) == "555"


def test_update_guess_channel_rejects_unknown_channel(authed_client, fake_ctx):
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=None)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    resp = authed_client.put("/api/config/guess", json={"channel_id": "555"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "not found" in data["detail"].lower()

