"""Tests for /api/config/* endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
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
    for section in ("global", "welcome", "xp", "prune", "spoiler", "moderation", "roles", "auto_delete"):
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


def test_update_xp_promotion_review_roles(authed_client, fake_ctx):
    """Grant role and ping role are separate keys — one must not write the other."""
    resp = authed_client.put("/api/config/xp", json={
        "promotion_review_grant_role_id": "5001",
        "promotion_review_ping_role_id": "5002",
    })
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "promotion_review_grant_role_id", "0", fake_ctx.guild_id) == "5001"
        assert get_config_value(conn, "promotion_review_ping_role_id", "0", fake_ctx.guild_id) == "5002"


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


def test_xp_retention_is_off_until_it_is_switched_on(authed_client, fake_ctx):
    """The dial that lets a million rows of member data be deleted.

    Its default has to be off, and it has to survive the round trip — a dial
    that reads back as off after being saved is indistinguishable from one
    nobody ever set, and this is not a knob to be wrong about quietly.
    """
    from bot_modules.services import xp_rollup_service

    section = authed_client.get("/api/config").json()["xp"]
    assert section["xp_retention_enabled"] == "0"
    assert section["xp_retention_days"] == xp_rollup_service.RAW_RETENTION_DAYS
    assert section["xp_retention_prunable"] == 0

    assert authed_client.put(
        "/api/config/xp", json={"xp_retention_enabled": "1"}
    ).status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert xp_rollup_service.retention_enabled(conn, fake_ctx.guild_id) is True
    assert authed_client.get("/api/config").json()["xp"]["xp_retention_enabled"] == "1"

    assert authed_client.put(
        "/api/config/xp", json={"xp_retention_enabled": "0"}
    ).status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert xp_rollup_service.retention_enabled(conn, fake_ctx.guild_id) is False


def test_xp_retention_stores_only_one_or_zero(authed_client, fake_ctx):
    """Anything the reader wouldn't recognise is normalised, not stored raw.

    retention_enabled() treats an unrecognised value as off, so persisting the
    caller's spelling would let a deliberate opt-in look like a silent failure.
    """
    from bot_modules.core.db_utils import get_config_value
    from bot_modules.services import xp_rollup_service

    assert authed_client.put(
        "/api/config/xp", json={"xp_retention_enabled": "yes please"}
    ).status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        stored = get_config_value(
            conn, xp_rollup_service.RETENTION_CONFIG_KEY, "0", fake_ctx.guild_id
        )
    assert stored == "0"


def test_update_xp_rejects_zero_voice_interval(authed_client, fake_ctx):
    # A 0-second interval divides by zero in completed_voice_intervals and
    # would kill voice XP guild-wide — the model must reject it (422).
    resp = authed_client.put("/api/config/xp", json={"voice_interval_seconds": 0})
    assert resp.status_code == 422
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        from bot_modules.core.xp_system import _XP_COEFF_PREFIX
        # Nothing persisted.
        assert get_config_value(
            conn, f"{_XP_COEFF_PREFIX}voice_interval_seconds", "", fake_ctx.guild_id
        ) == ""


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


# ── PUT /api/config/nsfw-classifier ──────────────────────────────────


def test_update_nsfw_classifier(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/nsfw-classifier",
        json={
            "threshold": 0.4,
            "sfw_threshold": 0.85,
            "sfw_mode": "log",
            "sfw_log_channel_id": "777",
            "sfw_exempt_channels": ["1001"],
        },
    )
    assert resp.status_code == 200

    from bot_modules.services.nsfw_classifier_service import (
        load_settings,
        load_sfw_policy,
    )

    threshold, sfw_threshold = load_settings(fake_ctx.db_path, fake_ctx.guild_id)
    assert (threshold, sfw_threshold) == (0.4, 0.85)

    policy = load_sfw_policy(fake_ctx.db_path, fake_ctx.guild_id)
    assert policy.mode == "log"
    assert policy.log_channel_id == 777
    assert policy.exempt_channel_ids == frozenset({1001})


def test_update_nsfw_observe_toggle(authed_client, fake_ctx):
    # The panel writes this alongside the thresholds; the service reads it on
    # every age-gated image message, so a round trip through both is the test.
    from bot_modules.services.nsfw_classifier_service import load_observe_policy

    assert load_observe_policy(fake_ctx.db_path, fake_ctx.guild_id) is False

    resp = authed_client.put(
        "/api/config/nsfw-classifier", json={"observe_age_gated": True}
    )
    assert resp.status_code == 200
    assert load_observe_policy(fake_ctx.db_path, fake_ctx.guild_id) is True

    authed_client.put(
        "/api/config/nsfw-classifier", json={"observe_age_gated": False}
    )
    assert load_observe_policy(fake_ctx.db_path, fake_ctx.guild_id) is False


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"threshold": 0}, id="threshold-zero"),
        pytest.param({"threshold": 1.5}, id="threshold-above-one"),
        pytest.param({"sfw_threshold": -0.1}, id="sfw-threshold-negative"),
        pytest.param({"sfw_mode": "delete-everything"}, id="unknown-mode"),
    ],
)
def test_nsfw_classifier_rejects_inert_settings(authed_client, body):
    # Each of these would silently disable a gate rather than loosen it, so
    # they're refused at write time as well as ignored at read time.
    assert authed_client.put("/api/config/nsfw-classifier", json=body).status_code == 400


def test_nsfw_classifier_metrics_empty(authed_client):
    resp = authed_client.get("/api/nsfw-classifier/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["classified"] == 0
    assert body["labels"] == []


# ── PUT /api/config/auto-react/{channel_id} ──────────────────────────


def test_auto_react_rule_with_tips_and_rungs(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/auto-react/4242",
        json={
            "emojis": ["🔥", "💎"],
            "enabled": True,
            "tips_enabled": True,
            "rungs": {"🔥": 5, "💎": 25},
        },
    )
    assert resp.status_code == 200

    from bot_modules.services.reaction_tip_service import get_rungs

    assert get_rungs(fake_ctx.db_path, fake_ctx.guild_id, 4242) == {"🔥": 5, "💎": 25}

    section = authed_client.get("/api/config").json()["auto_react"]
    rule = next(r for r in section if r["channel_id"] == "4242")
    assert rule["tips_enabled"] is True
    assert rule["rungs"] == {"🔥": 5, "💎": 25}


def test_auto_react_rejects_a_one_coin_rung(authed_client):
    # After the 1-coin minimum rake a 1-coin rung delivers the poster nothing,
    # so it's refused rather than silently declining every tap.
    resp = authed_client.put(
        "/api/config/auto-react/4242",
        json={"emojis": ["🔥"], "tips_enabled": True, "rungs": {"🔥": 1}},
    )
    assert resp.status_code == 400


def test_auto_react_drops_rungs_for_removed_emoji(authed_client, fake_ctx):
    from bot_modules.services.reaction_tip_service import get_rungs

    authed_client.put(
        "/api/config/auto-react/4242",
        json={"emojis": ["🔥", "💎"], "tips_enabled": True, "rungs": {"🔥": 5, "💎": 25}},
    )
    # 💎 drops off the rule — its price must not linger and stay chargeable.
    authed_client.put(
        "/api/config/auto-react/4242",
        json={"emojis": ["🔥"], "tips_enabled": True, "rungs": {"🔥": 5}},
    )

    assert get_rungs(fake_ctx.db_path, fake_ctx.guild_id, 4242) == {"🔥": 5}


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
    assert v["submit_max_per_window"] == 5
    assert v["submit_window_seconds"] == 3600
    assert v["max_guesses_per_round"] == 5


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
    assert resp.status_code == 400
    assert "crop_difficulty" in resp.json()["detail"]


def test_update_guess_rate_limit_fields(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/guess",
        json={
            "submit_max_per_window": 2,
            "submit_window_seconds": 600,
            "max_guesses_per_round": 3,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "guess_submit_max_per_window", "5", fake_ctx.guild_id) == "2"
        assert get_config_value(conn, "guess_submit_window_seconds", "3600", fake_ctx.guild_id) == "600"
        assert get_config_value(conn, "guess_max_guesses_per_round", "5", fake_ctx.guild_id) == "3"


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


def test_get_config_includes_risky_section_defaults(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    r = resp.json()["risky"]
    assert r["ping_role_id"] == "0"
    assert r["min_game_seconds"] == 0
    assert r["max_games_per_channel"] == 10


def test_get_config_includes_pen_pals_timer_defaults(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    pp = resp.json()["pen_pals"]
    assert pp["session_seconds"] == 86400
    assert pp["match_cooldown_seconds"] == 2592000
    assert pp["max_question_swaps"] == 3
    assert pp["warn_seconds"] == 3600
    assert pp["question_suppress_seconds"] == 7200
    # Reply reminders ship off — a guild only starts nudging once a mod says so.
    assert pp["reply_reminder_seconds"] == 0


def test_update_pen_pals_timers_persists(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/pen-pals/timers",
        json={
            "session_seconds": 1800,
            "match_cooldown_seconds": 86400,
            "max_question_swaps": 1,
            "warn_seconds": 300,
            "question_suppress_seconds": 600,
            "reply_reminder_seconds": 21600,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp2 = authed_client.get("/api/config")
    pp = resp2.json()["pen_pals"]
    assert pp["session_seconds"] == 1800
    assert pp["match_cooldown_seconds"] == 86400
    assert pp["max_question_swaps"] == 1
    assert pp["warn_seconds"] == 300
    assert pp["question_suppress_seconds"] == 600
    assert pp["reply_reminder_seconds"] == 21600


def test_update_pen_pals_timers_rejects_a_negative_reply_reminder(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/pen-pals/timers",
        json={
            "session_seconds": 1800,
            "match_cooldown_seconds": 86400,
            "max_question_swaps": 1,
            "warn_seconds": 300,
            "question_suppress_seconds": 600,
            "reply_reminder_seconds": -60,
        },
    )
    assert resp.status_code == 400


def test_get_config_pen_pals_room_visibility_defaults_to_mods(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["pen_pals"]["room_visibility"] == "mods"


def test_update_pen_pals_config_persists_room_visibility(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/pen-pals",
        json={"enabled": True, "room_visibility": "everyone"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    pp = authed_client.get("/api/config").json()["pen_pals"]
    assert pp["room_visibility"] == "everyone"


def test_update_pen_pals_config_normalizes_bad_room_visibility(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/pen-pals",
        json={"enabled": True, "room_visibility": "nonsense"},
    )
    assert resp.status_code == 200
    pp = authed_client.get("/api/config").json()["pen_pals"]
    assert pp["room_visibility"] == "mods"


def test_get_config_pen_pals_intro_message_defaults_to_empty(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["pen_pals"]["intro_message"] == ""


def test_update_pen_pals_config_persists_intro_message(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/pen-pals",
        json={"enabled": True, "intro_message": "  Be kind to your pen pal!  "},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    pp = authed_client.get("/api/config").json()["pen_pals"]
    assert pp["intro_message"] == "Be kind to your pen pal!"


def test_pen_pals_pool_events_are_newest_first(authed_client, fake_ctx):
    """The history behind the Pool Activity list. Without it the dashboard can
    only show who is waiting right now, so a pool that has gone flat looks the
    same as one nobody ever joined."""
    from bot_modules.cogs.pen_pals_cog import _record_pool_event

    with fake_ctx.open_db() as conn:
        _record_pool_event(conn, fake_ctx.guild_id, 4242, "join", "panel", at=100.0)
        _record_pool_event(conn, fake_ctx.guild_id, 4242, "leave", "matched", at=200.0)

    events = authed_client.get("/api/config/pen-pals/pool-events").json()["events"]

    assert [(e["action"], e["reason"]) for e in events] == [
        ("leave", "matched"),
        ("join", "panel"),
    ]
    # Snowflakes cross as strings so JS float math can't round them.
    assert events[0]["user_id"] == "4242"


def test_pen_pals_pool_events_carry_the_current_opt_outs(authed_client, fake_ctx):
    """The log says a member stopped being re-pooled; only this says they will
    stay that way. Read-only — there is deliberately no route for staff to
    clear a member's own opt-out."""
    from bot_modules.cogs.pen_pals_cog import _set_opt_out

    with fake_ctx.open_db() as conn:
        _set_opt_out(conn, fake_ctx.guild_id, 4242, at=100.0)
        _set_opt_out(conn, fake_ctx.guild_id + 1, 77, at=200.0)  # another guild

    optouts = authed_client.get("/api/config/pen-pals/pool-events").json()["optouts"]

    assert optouts == [{"user_id": "4242", "at": 100.0}]


def test_pen_pals_pool_events_are_scoped_to_the_active_guild(authed_client, fake_ctx):
    from bot_modules.cogs.pen_pals_cog import _record_pool_event

    with fake_ctx.open_db() as conn:
        _record_pool_event(conn, fake_ctx.guild_id + 1, 99, "join", "panel", at=100.0)

    assert authed_client.get("/api/config/pen-pals/pool-events").json()["events"] == []


def test_update_pen_pals_config_rejects_oversized_intro_message(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/pen-pals",
        json={"enabled": True, "intro_message": "x" * 1001},
    )
    assert resp.status_code == 400


def test_update_pen_pals_timers_rejects_invalid_session_seconds(authed_client):
    resp = authed_client.put(
        "/api/config/pen-pals/timers",
        json={
            "session_seconds": 0,
            "match_cooldown_seconds": 0,
            "max_question_swaps": 0,
            "warn_seconds": 0,
            "question_suppress_seconds": 0,
        },
    )
    assert resp.status_code == 400


def test_update_pen_pals_separations_persists_normalized(authed_client):
    # Big snowflake-ish ids to confirm they round-trip as strings.
    a, b = "1469000000000000123", "1469000000000000456"
    resp = authed_client.put(
        "/api/config/pen-pals/separations",
        json={"separations": [{"user_a": b, "user_b": a}]},  # entered high→low
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    pp = authed_client.get("/api/config").json()["pen_pals"]
    assert pp["separations"] == [{"user_a": a, "user_b": b}]  # normalized low→high

    # Sending an empty list clears them.
    resp2 = authed_client.put("/api/config/pen-pals/separations", json={"separations": []})
    assert resp2.status_code == 200
    assert authed_client.get("/api/config").json()["pen_pals"]["separations"] == []


def test_update_pen_pals_separations_rejects_self_pair(authed_client):
    resp = authed_client.put(
        "/api/config/pen-pals/separations",
        json={"separations": [{"user_a": "42", "user_b": "42"}]},
    )
    assert resp.status_code == 400


# ── /config/risky — in-memory state must be updated ──────────────────


def test_update_risky_persists_and_updates_in_memory_state(authed_client, fake_ctx):
    """The risky_roll in-memory `state.ping_roles` dict must reflect the write."""
    from bot_modules.services.risky_roll import state as rr_state

    rr_state.ping_roles.pop(fake_ctx.guild_id, None)
    rr_state.min_game_seconds.pop(fake_ctx.guild_id, None)
    rr_state.max_games_per_channel.pop(fake_ctx.guild_id, None)

    resp = authed_client.put(
        "/api/config/risky",
        json={"ping_role_id": "5555", "min_game_seconds": 90, "max_games_per_channel": 4},
    )
    assert resp.status_code == 200

    assert rr_state.ping_roles[fake_ctx.guild_id] == 5555
    assert rr_state.min_game_seconds[fake_ctx.guild_id] == 90
    assert rr_state.max_games_per_channel[fake_ctx.guild_id] == 4


def test_update_risky_rejects_max_games_below_one(authed_client):
    resp = authed_client.put(
        "/api/config/risky", json={"max_games_per_channel": 0}
    )
    assert resp.status_code == 400


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
    cog.post_panel = AsyncMock(return_value=88888)
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


def test_dms_post_panel_invokes_post_panel_and_persists_ids(authed_client, fake_ctx):
    """Happy path: cog.post_panel(guild, channel_id) is
    awaited; the returned message_id is persisted via set_panel_settings; and
    the cog's in-memory panel_settings dict is updated."""
    cog, guild = _dms_panel_guild(fake_ctx)

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 200
    cog.post_panel.assert_awaited_once_with(guild, 5000)
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
    cog.post_panel.assert_not_awaited()


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
    cog.post_panel.assert_not_awaited()
    assert cog.panel_settings == {}


def test_dms_post_panel_502_when_discord_rejects_send(authed_client, fake_ctx):
    """Perms look fine but the actual send still fails (post_panel → None):
    the route must report an error instead of a false success."""
    cog, guild = _dms_panel_guild(fake_ctx)
    cog.post_panel.return_value = None

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 502
    assert cog.panel_settings == {}


# ── /config/welcome/preview — admin-only render ──────────────────────


def test_welcome_preview_503_when_bot_unavailable(authed_client):
    resp = authed_client.post("/api/config/welcome/preview", json={})
    assert resp.status_code == 503


def _welcome_preview_guild(fake_ctx):
    """Wire a minimal bot/guild/member mock good enough for the preview route."""
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
    return guild


def _post_preview(authed_client, payload):
    import discord
    from unittest.mock import AsyncMock, patch

    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        return authed_client.post("/api/config/welcome/preview", json=payload)


def test_welcome_preview_renders_with_bot_member(authed_client, fake_ctx):
    """When the bot guild and the auth user are reachable, preview returns
    rendered embed dicts for both welcome and leave."""
    _welcome_preview_guild(fake_ctx)

    resp = _post_preview(authed_client, {})
    # 200 happy path; the route's build_*_embed helpers are exercised. If the
    # preview shape changes later, this is a useful regression bait.
    assert resp.status_code == 200
    body = resp.json()
    assert "welcome" in body
    assert "leave" in body


def test_welcome_preview_uses_posted_values_over_stored(authed_client, fake_ctx):
    """W-C3: the preview must reflect the form's current (unsaved) edits, not
    the stored config — posted welcome/leave messages win over saved ones."""
    from bot_modules.core.db_utils import set_config_value

    _welcome_preview_guild(fake_ctx)

    # Store different values so we can tell which source the preview used.
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "welcome_message", "STORED WELCOME", fake_ctx.guild_id)
        set_config_value(conn, "leave_message", "STORED LEAVE", fake_ctx.guild_id)

    resp = _post_preview(
        authed_client,
        {
            "welcome_message": "UNSAVED WELCOME {member_name}",
            "leave_message": "UNSAVED LEAVE",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "UNSAVED WELCOME" in body["welcome"]["description"]
    assert "STORED WELCOME" not in body["welcome"]["description"]
    assert "UNSAVED LEAVE" in body["leave"]["description"]
    # Placeholders in posted values still resolve.
    assert "tester" in body["welcome"]["description"]


def test_welcome_preview_falls_back_to_stored_values(authed_client, fake_ctx):
    """Fields omitted from the POST body fall back to the saved config."""
    from bot_modules.core.db_utils import set_config_value

    _welcome_preview_guild(fake_ctx)

    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "welcome_message", "STORED WELCOME", fake_ctx.guild_id)

    resp = _post_preview(authed_client, {"leave_message": "UNSAVED LEAVE"})
    assert resp.status_code == 200
    body = resp.json()
    assert "STORED WELCOME" in body["welcome"]["description"]
    assert "UNSAVED LEAVE" in body["leave"]["description"]


# ── PUT /api/config/guess — channel selection ──────────────────────────
#
# Guess accepts SFW and NSFW submissions alike, so the channel is NOT required
# to be age-gated: the age-gate check that used to live here was removed with
# SFW support, and placement is a moderator call. The runtime has had no
# is_nsfw() recheck since 47ca6a5, so this route was the last holdout. Only the
# channel's existence is still validated.


def _attach_guess_bot(fake_ctx, *, nsfw: bool):
    channel = MagicMock()
    channel.is_nsfw = MagicMock(return_value=nsfw)
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=channel)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot


@pytest.mark.parametrize("nsfw", [True, False], ids=["age_gated", "sfw"])
def test_update_guess_channel_saves_regardless_of_age_gate(authed_client, fake_ctx, nsfw):
    """Both an age-gated and a plain SFW channel save.

    The SFW row is the regression: it returned 400 "Guess only posts in
    age-gated channels" before SFW submissions were supported.
    """
    _attach_guess_bot(fake_ctx, nsfw=nsfw)

    resp = authed_client.put("/api/config/guess", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "guess_channel_id", "0", fake_ctx.guild_id) == "555"


def test_update_guess_channel_disabled_sentinel_saves(authed_client, fake_ctx):
    # "0" is the "(disabled)" option; it must persist without tripping the
    # channel-not-found lookup that a real channel id would.
    _attach_guess_bot(fake_ctx, nsfw=False)

    resp = authed_client.put("/api/config/guess", json={"channel_id": "0"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "guess_channel_id", "x", fake_ctx.guild_id) == "0"


def test_update_guess_channel_rejects_unknown_channel(authed_client, fake_ctx):
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=None)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    resp = authed_client.put("/api/config/guess", json={"channel_id": "555"})
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()


# ── Bot-identity avatar_url SSRF guard ────────────────────────────────
#
# POST /config/bot-identity fetches a caller-supplied avatar_url server-side.
# Without a scheme allowlist + private-range rejection, file://, loopback and
# link-local (cloud-metadata) URLs all reach the fetch — a classic SSRF.


def test_reject_unsafe_avatar_url_blocks_non_http_scheme():
    import pytest
    from fastapi import HTTPException

    from web_server.routes.config import _reject_unsafe_avatar_url

    with pytest.raises(HTTPException) as exc:
        _reject_unsafe_avatar_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_reject_unsafe_avatar_url_blocks_loopback_and_link_local():
    import pytest
    from fastapi import HTTPException

    from web_server.routes.config import _reject_unsafe_avatar_url

    for bad in (
        "http://127.0.0.1/x.png",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/x.png",
        "http://[::1]/x.png",
    ):
        with pytest.raises(HTTPException) as exc:
            _reject_unsafe_avatar_url(bad)
        assert exc.value.status_code == 400, bad


def test_reject_unsafe_avatar_url_allows_public_ip():
    # A public literal must pass the guard (no exception raised).
    from web_server.routes.config import _reject_unsafe_avatar_url

    _reject_unsafe_avatar_url("https://93.184.216.34/avatar.png")


async def test_download_avatar_bytes_rejects_loopback():
    import pytest
    from fastapi import HTTPException

    from web_server.routes.config import _download_avatar_bytes

    with pytest.raises(HTTPException) as exc:
        await _download_avatar_bytes("http://127.0.0.1:80/evil.png")
    assert exc.value.status_code == 400


# ── Birthday calendar anchors on the guild-local date ─────────────────
#
# The calendar's "today" must come from the guild's tz offset, not the host's
# clock — otherwise days_until flips at host midnight instead of the guild's.


def test_birthday_calendar_uses_guild_local_today(authed_client, fake_ctx):
    from datetime import date

    from bot_modules.core.db_utils import set_config_value

    # A large offset (>1 full day) shifts guild-local "today" deterministically
    # ahead of the host date, avoiding any midnight-window flakiness.
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "tz_offset_hours", "50", fake_ctx.guild_id)  # +2d2h
        host_today = date.today()
        conn.execute(
            "INSERT INTO member_birthdays"
            " (guild_id, user_id, birth_month, birth_day, set_by, set_at, preference)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fake_ctx.guild_id, 4242, host_today.month, host_today.day, 1, 0.0, "public"),
        )

    resp = authed_client.get("/api/birthday/calendar", params={"days": 400})
    assert resp.status_code == 200
    entries = {int(e["user_id"]): e for e in resp.json()}
    assert 4242 in entries
    # Guild-local today is ~2 days ahead of the host date, so a birthday on the
    # host date already passed this year → it rolls to next year (~363 days).
    # With the buggy host-date anchor this would be 0.
    assert entries[4242]["days_until"] >= 360



# ── /api/config/intake ────────────────────────────────────────────────


def test_intake_config_defaults_in_get(authed_client):
    resp = authed_client.get("/api/config")
    assert resp.status_code == 200
    intake = resp.json()["intake"]
    assert intake["enabled"] is False
    assert intake["channel_id"] == "0"
    assert intake["verified_role_id"] == "0"
    # Derived, read-only: nothing configured → no channel counts as greeting.
    assert intake["greet_channel_id"] == "0"
    assert intake["stale_hours"] == 24
    # Effective (default) step list is surfaced so the editor never starts blank.
    keys = [s["key"] for s in intake["steps"]]
    assert keys == [
        "greeted", "verified", "member_role",
        "sfw_questions", "nsfw_role", "nsfw_questions",
    ]
    # Snowflake-ish ids come back as strings.
    assert all(isinstance(s["role_id"], str) for s in intake["steps"])


def test_update_intake_roundtrips(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/intake", json={
        "enabled": True,
        "channel_id": "5551234",
        "verified_role_id": "903888888888888888",
        "completion_code": "  DK-7734  ",
        "stale_hours": 6,
        "steps": [
            {"key": "", "label": "Greeted", "auto": "greeted", "role_id": "0"},
            {"key": "", "label": "Member role", "auto": "role_gained", "role_id": "901"},
            {"key": "", "label": "SFW questions asked", "auto": "", "role_id": "0"},
        ],
    })
    assert resp.status_code == 200
    resp = authed_client.get("/api/config")
    intake = resp.json()["intake"]
    assert intake["enabled"] is True
    assert intake["channel_id"] == "5551234"
    assert intake["completion_code"] == "DK-7734"  # stored stripped
    # Snowflake round-trips as a string — never through parseInt/JSON number.
    assert intake["verified_role_id"] == "903888888888888888"
    assert intake["stale_hours"] == 6
    assert [s["key"] for s in intake["steps"]] == [
        "greeted", "member_role", "sfw_questions_asked",
    ]
    assert intake["steps"][1]["role_id"] == "901"

    # The bot-side reader sees the same effective steps.
    from bot_modules.services import intake_service as svc
    with open_db(fake_ctx.db_path) as conn:
        assert svc.is_enabled(conn, fake_ctx.guild_id) is True
        assert [s.key for s in svc.step_config(conn, fake_ctx.guild_id)] == [
            "greeted", "member_role", "sfw_questions_asked",
        ]
        # …and reads the saved verified role as a verification signal.
        assert svc.verification_signalled(
            conn, fake_ctx.guild_id, [903888888888888888], False
        ) is True


def test_update_intake_rejects_bad_steps(authed_client):
    base = {"steps": [{"key": "", "label": "", "auto": "", "role_id": "0"}]}
    assert authed_client.put("/api/config/intake", json=base).status_code == 422
    # role_gained without a role would be an inert step — rejected, not stored.
    resp = authed_client.put("/api/config/intake", json={
        "steps": [{"key": "", "label": "NSFW", "auto": "role_gained", "role_id": "0"}],
    })
    assert resp.status_code == 422
    resp = authed_client.put("/api/config/intake", json={
        "steps": [{"key": "", "label": "X", "auto": "telepathy", "role_id": "0"}],
    })
    assert resp.status_code == 422
    assert authed_client.put(
        "/api/config/intake", json={"steps": []}
    ).status_code == 422
    assert authed_client.put(
        "/api/config/intake", json={"stale_hours": 0}
    ).status_code == 422


def test_update_intake_roundtrips_step_codes(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/intake", json={
        "completion_code": "DK-DONE",
        "steps": [
            {"key": "", "label": "SFW questions", "code": "  DK-SFW  "},
            {"key": "", "label": "NSFW questions", "code": "DK-NSFW"},
            {"key": "", "label": "Greeted", "auto": "greeted"},
        ],
    })
    assert resp.status_code == 200
    intake = authed_client.get("/api/config").json()["intake"]
    assert [s["code"] for s in intake["steps"]] == ["DK-SFW", "DK-NSFW", ""]

    from bot_modules.services import intake_service as svc
    with open_db(fake_ctx.db_path) as conn:
        assert [s.code for s in svc.step_config(conn, fake_ctx.guild_id)] == [
            "DK-SFW", "DK-NSFW", "",
        ]


def test_update_intake_rejects_codes_that_contain_each_other(authed_client, fake_ctx):
    # Codes match by containment, so posting "DK-SFW-DONE" would silently tick
    # a "DK-SFW" step too.
    resp = authed_client.put("/api/config/intake", json={
        "steps": [
            {"key": "", "label": "SFW questions", "code": "DK-SFW"},
            {"key": "", "label": "SFW wrap-up", "code": "dk-sfw-done"},
        ],
    })
    assert resp.status_code == 422
    assert "SFW wrap-up" in resp.json()["detail"]
    # Nothing was written — the reject lands before any config write.
    from bot_modules.services import intake_service as svc
    with open_db(fake_ctx.db_path) as conn:
        assert svc.step_config(conn, fake_ctx.guild_id) == list(svc.DEFAULT_STEPS)

    # A step code clashing with the guild-wide completion code is rejected too,
    # whether the completion code arrives in the same request…
    assert authed_client.put("/api/config/intake", json={
        "completion_code": "DK",
        "steps": [{"key": "", "label": "SFW", "code": "DK-SFW"}],
    }).status_code == 422
    # …or is already stored from an earlier save.
    assert authed_client.put(
        "/api/config/intake", json={"completion_code": "DK"}
    ).status_code == 200
    assert authed_client.put("/api/config/intake", json={
        "steps": [{"key": "", "label": "SFW", "code": "DK-SFW"}],
    }).status_code == 422
    # An over-long code is rejected rather than silently truncated.
    assert authed_client.put("/api/config/intake", json={
        "steps": [{"key": "", "label": "SFW", "code": "x" * 81}],
    }).status_code == 422


def test_update_intake_guards_a_completion_code_only_save(authed_client, fake_ctx):
    # Regression: the clash check used to run only when the body carried
    # steps, so setting just the completion code walked a clash straight in —
    # and a completion code containing a step's code is the worst kind, since
    # pasting that step's message closes the card and skips everything else.
    assert authed_client.put("/api/config/intake", json={
        "steps": [{"key": "", "label": "SFW questions", "code": "DK-SFW"}],
    }).status_code == 200
    resp = authed_client.put("/api/config/intake", json={"completion_code": "DK"})
    assert resp.status_code == 422
    assert "DK-SFW" in resp.json()["detail"]

    from bot_modules.services import intake_service as svc
    with open_db(fake_ctx.db_path) as conn:
        assert svc.completion_code(conn, fake_ctx.guild_id) == ""
    # A non-overlapping completion code still saves fine.
    assert authed_client.put(
        "/api/config/intake", json={"completion_code": "ALL-WELCOMED"}
    ).status_code == 200


def test_update_intake_dedupes_generated_keys(authed_client):
    resp = authed_client.put("/api/config/intake", json={
        "steps": [
            {"key": "", "label": "Questions", "auto": "", "role_id": "0"},
            {"key": "", "label": "Questions", "auto": "", "role_id": "0"},
        ],
    })
    assert resp.status_code == 200
    intake = authed_client.get("/api/config").json()["intake"]
    assert [s["key"] for s in intake["steps"]] == ["questions", "questions_2"]


# ── /api/config/intake/reference ──────────────────────────────────────


def test_intake_reference_roundtrips_without_bot(authed_client):
    blocks = [
        {"kind": "text", "title": "How it works", "body": "Greet them."},
        {"kind": "questions", "title": "SFW", "body": "Q1?\nQ2?"},
    ]
    resp = authed_client.put("/api/config/intake/reference", json={
        "channel_id": "424242",
        "blocks": blocks,
    })
    assert resp.status_code == 200
    # FakeCtx has no bot — config is saved, sync gracefully skipped.
    assert resp.json()["sync"]["synced"] is False

    intake = authed_client.get("/api/config").json()["intake"]
    assert intake["reference_channel_id"] == "424242"
    assert intake["reference_blocks"] == blocks


def test_intake_reference_rejects_bad_blocks(authed_client):
    resp = authed_client.put("/api/config/intake/reference", json={
        "blocks": [{"kind": "telepathy", "title": "x", "body": "y"}],
    })
    assert resp.status_code == 422
    resp = authed_client.put("/api/config/intake/reference", json={
        "blocks": [{"kind": "questions", "title": "t", "body": "  \n "}],
    })
    assert resp.status_code == 422


def test_intake_reference_import_requires_bot(authed_client):
    resp = authed_client.post("/api/config/intake/reference/import", json={
        "channel_id": "424242",
    })
    assert resp.status_code == 503  # FakeCtx has no connected bot


def test_update_intake_normalizes_keys_for_button_dispatch(authed_client):
    # Keys land in persistent-button custom_ids and must fullmatch the
    # dispatch template [\w-]{1,64} after a restart — long label slugs are
    # capped and bad charsets normalized (regression: 80-char labels made
    # buttons dead after restart).
    import re as _re
    long_label = "x" * 80
    resp = authed_client.put("/api/config/intake", json={
        "steps": [
            {"key": "", "label": long_label, "auto": "", "role_id": "0"},
            {"key": "has:colon and spaces", "label": "Colons", "auto": "", "role_id": "0"},
            {"key": "", "label": long_label + "b", "auto": "", "role_id": "0"},
        ],
    })
    assert resp.status_code == 200
    keys = [s["key"] for s in authed_client.get("/api/config").json()["intake"]["steps"]]
    assert all(_re.fullmatch(r"[\w-]{1,64}", k) for k in keys)
    assert len(set(keys)) == 3  # dedupe survived the cap
# ── Bump Tracker ──────────────────────────────────────────────────────
#
# The reminder loop is driven entirely by this config, and until the dashboard
# panel shipped these routes had no coverage at all. They are the only way to
# set up the feature, so the round-trip is worth pinning.


def test_bump_tracker_config_round_trip(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/bump-tracker", json={
        "channel_id": "7001", "role_id": "7002", "enabled": True,
    })
    assert resp.status_code == 200

    section = authed_client.get("/api/config").json()["bump_tracker"]
    assert section["configured"] is True
    assert section["enabled"] is True
    # Snowflakes must survive as strings — a bare JSON number loses precision.
    assert section["channel_id"] == "7001"
    assert section["role_id"] == "7002"
    assert isinstance(section["channel_id"], str)


def test_bump_tracker_site_add_update_and_delete(authed_client, fake_ctx):
    resp = authed_client.put("/api/config/bump-tracker/sites/disboard", json={
        "cooldown_hours": 2, "detector_bot_id": "302050872383242240",
    })
    assert resp.status_code == 200

    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    site = next(s for s in sites if s["site_name"] == "disboard")
    assert site["cooldown_seconds"] == 7200
    assert site["detector_bot_id"] == "302050872383242240"
    # Never bumped: the panel shows it as ready to bump right now.
    assert site["bumped_at"] is None
    assert site["ready"] is True

    # Re-PUT is an update, not a duplicate row.
    authed_client.put("/api/config/bump-tracker/sites/disboard", json={"cooldown_hours": 6})
    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    matching = [s for s in sites if s["site_name"] == "disboard"]
    assert len(matching) == 1
    assert matching[0]["cooldown_seconds"] == 21600

    resp = authed_client.delete("/api/config/bump-tracker/sites/disboard")
    assert resp.status_code == 200
    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    assert not [s for s in sites if s["site_name"] == "disboard"]


def test_bump_tracker_site_save_preserves_patterns_it_omits(authed_client, fake_ctx):
    """An omitted pattern must survive the save.

    A dashboard tab left open across a restart still runs the pre-137 JS, which
    sends no failure_pattern at all. Clearing it on save would turn the listing
    bot's "already bumped recently" refusal back into a recorded bump.
    """
    authed_client.put("/api/config/bump-tracker/sites/discadia", json={
        "cooldown_hours": 24,
        "detector_bot_id": "1222548162741538938",
        "detector_pattern": "bump done",
        "failure_pattern": "already bumped recently",
    })

    # The old panel's payload shape: cooldown and bot id only.
    resp = authed_client.put("/api/config/bump-tracker/sites/discadia", json={
        "cooldown_hours": 12, "detector_bot_id": "1222548162741538938",
    })
    assert resp.status_code == 200

    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    site = next(s for s in sites if s["site_name"] == "discadia")
    assert site["cooldown_seconds"] == 43200
    assert site["detector_pattern"] == "bump done"
    assert site["failure_pattern"] == "already bumped recently"


def test_bump_tracker_site_save_can_still_clear_a_pattern(authed_client, fake_ctx):
    """Omitted means keep, but an explicit empty string still clears."""
    authed_client.put("/api/config/bump-tracker/sites/discadia", json={
        "cooldown_hours": 24, "failure_pattern": "already bumped recently",
    })
    authed_client.put("/api/config/bump-tracker/sites/discadia", json={
        "cooldown_hours": 24, "failure_pattern": "",
    })

    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    site = next(s for s in sites if s["site_name"] == "discadia")
    assert site["failure_pattern"] == ""


def test_bump_tracker_detector_route_preserves_the_failure_veto(authed_client, fake_ctx):
    """The detector route predates failure_pattern; no caller sends one."""
    authed_client.put("/api/config/bump-tracker/sites/discadia", json={
        "cooldown_hours": 24, "failure_pattern": "already bumped recently",
    })

    resp = authed_client.put("/api/config/bump-tracker/sites/discadia/detector", json={
        "detector_bot_id": "1222548162741538938",
    })
    assert resp.status_code == 200

    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    site = next(s for s in sites if s["site_name"] == "discadia")
    assert site["detector_bot_id"] == "1222548162741538938"
    assert site["failure_pattern"] == "already bumped recently"


def test_bump_tracker_rejects_an_overlong_pattern(authed_client, fake_ctx):
    """Patterns are substring-matched against every bot message; bound them."""
    resp = authed_client.put("/api/config/bump-tracker/sites/discadia", json={
        "cooldown_hours": 24, "failure_pattern": "x" * 201,
    })
    assert resp.status_code == 422


def test_bump_tracker_log_starts_the_cooldown(authed_client, fake_ctx):
    authed_client.put("/api/config/bump-tracker/sites/discadia", json={"cooldown_hours": 24})

    resp = authed_client.post("/api/config/bump-tracker/sites/discadia/log")
    assert resp.status_code == 200

    sites = authed_client.get("/api/config").json()["bump_tracker"]["sites"]
    site = next(s for s in sites if s["site_name"] == "discadia")
    assert site["bumped_at"] is not None
    assert site["ready"] is False
    # Just bumped, so nearly the whole 24h should remain.
    assert 23 * 3600 < site["seconds_remaining"] <= 24 * 3600


def test_bump_tracker_rejects_unknown_site(authed_client, fake_ctx):
    assert authed_client.post("/api/config/bump-tracker/sites/nope/log").status_code == 404
    resp = authed_client.put(
        "/api/config/bump-tracker/sites/nope/detector",
        json={"detector_bot_id": "123"},
    )
    assert resp.status_code == 404


# ── Branding: per-guild product names ──────────────────────────────────
#
# The casino's and the AI assistant's names used to be hardcoded, which showed
# the home server's branding to every other guild. They're branding_config
# columns now; blank means "use the built-in default".


def test_branding_section_reports_names_and_defaults(authed_client):
    br = authed_client.get("/api/config").json()["branding"]
    # Nothing set yet — empty overrides, defaults advertised for the placeholder.
    assert br["casino_name"] == ""
    assert br["assistant_name"] == ""
    assert br["default_casino_name"] == "Golden Meadow"
    assert br["default_assistant_name"] == "Billy-bot"


def test_branding_put_round_trips_names(authed_client):
    resp = authed_client.put(
        "/api/config/branding",
        json={"casino_name": "  Neon Pines  ", "assistant_name": "Sam-bot"},
    )
    assert resp.status_code == 200
    assert resp.json()["casino_name"] == "Neon Pines"  # trimmed
    assert resp.json()["assistant_name"] == "Sam-bot"

    br = authed_client.get("/api/config").json()["branding"]
    assert br["casino_name"] == "Neon Pines"
    assert br["assistant_name"] == "Sam-bot"


def test_branding_put_blank_name_falls_back_to_default(authed_client, fake_ctx):
    from bot_modules.services.branding_service import (
        DEFAULT_CASINO_NAME,
        resolve_casino_name,
    )

    authed_client.put("/api/config/branding", json={"casino_name": "Neon Pines"})
    resp = authed_client.put("/api/config/branding", json={"casino_name": "   "})
    assert resp.status_code == 200
    assert resp.json()["casino_name"] == ""

    assert authed_client.get("/api/config").json()["branding"]["casino_name"] == ""
    # And the bot-side resolver is back on the built-in name.
    assert resolve_casino_name(fake_ctx.db_path, fake_ctx.guild_id) == DEFAULT_CASINO_NAME


def test_branding_put_names_leave_the_accent_alone(authed_client):
    authed_client.put(
        "/api/config/branding", json={"accent_mode": "custom", "accent_hex": "#112233"}
    )
    authed_client.put("/api/config/branding", json={"casino_name": "Neon Pines"})

    br = authed_client.get("/api/config").json()["branding"]
    assert br["accent_mode"] == "custom"
    assert br["accent_hex"] == "#112233"
    assert br["casino_name"] == "Neon Pines"


def test_branding_put_rejects_an_overlong_name(authed_client):
    resp = authed_client.put(
        "/api/config/branding", json={"assistant_name": "x" * 200}
    )
    assert resp.status_code == 400


def test_branding_names_are_per_guild(fake_ctx):
    from bot_modules.services.branding_service import (
        DEFAULT_CASINO_NAME,
        resolve_casino_name,
    )

    client = _second_guild_client(fake_ctx)
    assert client.put(
        "/api/config/branding", json={"casino_name": "Neon Pines"}
    ).status_code == 200

    assert resolve_casino_name(fake_ctx.db_path, _SECOND_GUILD) == "Neon Pines"
    # The home guild keeps the built-in name.
    assert resolve_casino_name(fake_ctx.db_path, fake_ctx.guild_id) == DEFAULT_CASINO_NAME
# ── anonymity-config gating ────────────────────────────────────────────
# Confessions and Whisper both take a log_channel_id, and both logs record the
# author behind the anonymous message. Only an admin may point those logs
# somewhere — a game-host role holder redirecting them would de-anonymise
# every confession and whisper in the server.


def _game_host_client(fake_ctx, host_role_id: int = 4242) -> TestClient:
    """A non-admin caller holding the configured game-host role."""
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO games_editor_role (guild_id, role_id) VALUES (?, ?)",
            (fake_ctx.guild_id, host_role_id),
        )
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=2,
            username="host",
            access_token="token",
            permission_bits=0,  # explicitly not an administrator
            role_ids=[host_role_id],
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
        ),
    )
    return client


def test_game_host_cannot_redirect_the_confessions_log(fake_ctx):
    client = _game_host_client(fake_ctx)
    try:
        resp = client.put("/api/config/confessions", json={"log_channel_id": "9999"})
        assert resp.status_code == 403
    finally:
        client.close()


def test_game_host_cannot_redirect_the_whisper_log(fake_ctx):
    client = _game_host_client(fake_ctx)
    try:
        resp = client.put("/api/config/whisper", json={"log_channel_id": "9999"})
        assert resp.status_code == 403
    finally:
        client.close()


def test_game_host_can_still_edit_guess_config(fake_ctx):
    """The games-editor role keeps its legitimate reach — this isn't a blanket lockout."""
    client = _game_host_client(fake_ctx)
    try:
        resp = client.put("/api/config/guess", json={"enabled": True})
        assert resp.status_code != 403
    finally:
        client.close()


# ── Greeting Watch: extra greeting words round-trip ────────────────────


def test_greeting_watch_extra_words_roundtrip_and_bot_side_read(
    authed_client, fake_ctx
):
    """PUT normalizes the CSV (trim/lowercase/dedupe), GET returns the stored
    form, and the bot-side GuildConfig picks the tuple up after the cache
    invalidation the route performs."""
    resp = authed_client.put(
        "/api/config/greeting-watch",
        json={"extra_words": " Henlo ,henlo, GOOD Yawn ,\n o7 ,"},
    )
    assert resp.status_code == 200

    section = authed_client.get("/api/config").json()["greeting_watch"]
    assert section["extra_words"] == "henlo, good yawn, o7"

    cfg = fake_ctx.guild_config(fake_ctx.guild_id)
    assert cfg.greeting_watch_extra_words == ("henlo", "good yawn", "o7")


def test_greeting_watch_extra_words_empty_string_clears(authed_client, fake_ctx):
    authed_client.put(
        "/api/config/greeting-watch", json={"extra_words": "henlo"}
    )
    resp = authed_client.put("/api/config/greeting-watch", json={"extra_words": ""})
    assert resp.status_code == 200

    section = authed_client.get("/api/config").json()["greeting_watch"]
    assert section["extra_words"] == ""
    cfg = fake_ctx.guild_config(fake_ctx.guild_id)
    assert cfg.greeting_watch_extra_words == ()


# ── inactive channel setup + sweep (replaced /inactive panel|sweep) ──


def _inactive_bot(fake_ctx, *, channel_ok=True):
    """A live-bot stand-in with a text channel the setup route can reach."""
    import discord

    guild = MagicMock()
    guild.me = MagicMock()
    channel = MagicMock(spec=discord.TextChannel) if channel_ok else MagicMock(
        spec=discord.VoiceChannel
    )
    channel.name = "inactive"
    guild.get_channel.return_value = channel
    bot = MagicMock()
    bot.get_guild.return_value = guild
    fake_ctx.bot = bot
    return bot, guild, channel


def test_inactive_channel_setup_delegates_to_the_service(authed_client, fake_ctx, monkeypatch):
    """The route is plumbing; the four-step setup (persist, ensure role, grant
    access, revoke the old channel, post the panel) lives in the service so the
    auto-sweep and the dashboard can't drift apart."""
    import bot_modules.inactive.sweep_service as svc

    _inactive_bot(fake_ctx)
    called = {}

    async def _fake(ctx, guild, channel):
        called["hit"] = True
        return True, ""

    monkeypatch.setattr(svc, "setup_inactive_channel", _fake)
    r = authed_client.post("/api/config/inactive/channel", json={"channel_id": "5"})
    assert r.status_code == 200, r.text
    assert called["hit"]


def test_inactive_channel_setup_surfaces_the_failure_reason(authed_client, fake_ctx, monkeypatch):
    """"Missing Manage Roles" is fixable; a generic 500 isn't."""
    import bot_modules.inactive.sweep_service as svc

    _inactive_bot(fake_ctx)

    async def _fake(ctx, guild, channel):
        return False, "Missing **Manage Roles** — can't create the Inactive role."

    monkeypatch.setattr(svc, "setup_inactive_channel", _fake)
    r = authed_client.post("/api/config/inactive/channel", json={"channel_id": "5"})
    assert r.status_code == 400
    assert "Manage Roles" in r.json()["detail"]


def test_inactive_channel_setup_refuses_a_non_text_channel(authed_client, fake_ctx):
    _inactive_bot(fake_ctx, channel_ok=False)
    r = authed_client.post("/api/config/inactive/channel", json={"channel_id": "5"})
    assert r.status_code == 400
    assert "text channel" in r.json()["detail"].lower()


def test_running_the_sweep_needs_a_configured_channel(authed_client, fake_ctx):
    """Otherwise it would strip roles and move nobody anywhere."""
    _inactive_bot(fake_ctx)
    r = authed_client.post("/api/config/inactive/sweep")
    assert r.status_code == 400
    assert "no inactive channel" in r.json()["detail"].lower()


def test_running_the_sweep_reports_what_it_moved(authed_client, fake_ctx, monkeypatch):
    import bot_modules.inactive.sweep_service as svc

    _inactive_bot(fake_ctx)
    monkeypatch.setattr(svc, "read_inactive_channel_id", lambda ctx, gid: 99)

    async def _fake(ctx, guild, actor):
        return 3, 5, 2

    monkeypatch.setattr(svc, "run_inactive_sweep", _fake)
    r = authed_client.post("/api/config/inactive/sweep")
    assert r.status_code == 200, r.text
    body = r.json()
    # considered and overflow both reported: "moved 3" alone hides that two
    # more qualified but were held back by the per-run cap.
    assert (body["moved"], body["considered"], body["overflow"]) == (3, 5, 2)


def test_inactive_channel_setup_leaves_the_old_channel_alone_when_posting_fails(
    authed_client, fake_ctx, monkeypatch
):
    """Nothing destructive may run before the last thing that can fail.

    The revoke used to happen before the panel post, so a failed post (missing
    Embed Links, say) returned an error *after* the old channel's @Inactive
    access was already gone — reported as a failure while leaving the guild with
    no working inactive channel at all.
    """
    import asyncio

    import discord

    import bot_modules.inactive.sweep_service as svc

    guild = MagicMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "new-inactive"
    channel.set_permissions = AsyncMock()
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=403), "no embeds")
    )
    old_channel = MagicMock(spec=discord.TextChannel)
    old_channel.set_permissions = AsyncMock()
    guild.id = fake_ctx.guild_id
    guild.get_channel.side_effect = lambda cid: (
        channel if cid == 5 else old_channel
    )

    # setup_inactive_channel imports these inside the function, so patch them
    # where they're defined rather than on the calling module.
    monkeypatch.setattr(
        "bot_modules.inactive.apply.ensure_inactive_role",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "bot_modules.inactive.logic.stale_inactive_channel_id",
        lambda prev, new: 999,
    )
    monkeypatch.setattr(
        "bot_modules.core.branding.resolve_accent_color", AsyncMock(return_value=None)
    )

    ok, note = asyncio.run(svc.setup_inactive_channel(fake_ctx, guild, channel))

    assert ok is False
    assert "info panel" in note
    # The old channel keeps its access; the admin's previous setup still works.
    old_channel.set_permissions.assert_not_awaited()


# ── /config/dms/post-panel — the sticky-collision guard ───────────────


def _occupy_casino(fake_ctx, channel_id: int) -> None:
    """Put the casino hub — a bot-chasing panel — in a channel."""
    from bot_modules.core.db_utils import open_db

    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (fake_ctx.guild_id, "casino_panel_channel_id", str(channel_id)),
        )


def _occupy_pen_pals(fake_ctx, channel_id: int) -> None:
    """Put the pen pals panel — human-message-only — in a channel."""
    from bot_modules.core.db_utils import open_db

    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO pen_pals_config (guild_id, panel_channel_id) VALUES (?, ?)",
            (fake_ctx.guild_id, channel_id),
        )


def test_dms_post_panel_refuses_a_bot_chasing_channel(authed_client, fake_ctx):
    """The DM panel is the only route to a member's own DM settings, so
    burying it under a panel that re-takes the bottom on every repaint would
    take that surface away with nothing visible to explain it."""
    cog, _ = _dms_panel_guild(fake_ctx)
    _occupy_casino(fake_ctx, 5000)

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 400
    assert "casino hub panel" in resp.json()["detail"]
    cog.post_panel.assert_not_awaited()


def test_dms_post_panel_warns_beside_a_human_only_panel(authed_client, fake_ctx):
    cog, _ = _dms_panel_guild(fake_ctx)
    _occupy_pen_pals(fake_ctx, 5000)

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 200
    assert "pen pals panel" in resp.json()["warning"]
    cog.post_panel.assert_awaited_once()


def test_dms_post_panel_is_not_refused_on_account_of_itself(authed_client, fake_ctx):
    """Re-posting into the channel it already occupies is how an admin moves a
    panel that has drifted out of view."""
    cog, _ = _dms_panel_guild(fake_ctx)
    authed_client.post("/api/config/dms/post-panel", json={"channel_id": "5000"})

    resp = authed_client.post(
        "/api/config/dms/post-panel", json={"channel_id": "5000"}
    )
    assert resp.status_code == 200
    assert resp.json()["warning"] is None


# ── Voice transcription: only a downloaded model can be selected ──────


def test_voice_transcription_rejects_a_model_that_is_not_downloaded(
    authed_client, monkeypatch
):
    """The panel says a model has to read Downloaded before you can choose it;
    saving one that isn't only produced silent, member-invisible failures."""
    monkeypatch.setattr("web_server.routes.config._vt_model_is_cached", lambda m: False)

    resp = authed_client.put(
        "/api/config/voice-transcription",
        json={"enabled": True, "model_name": "tiny.en", "channel_ids": []},
    )
    assert resp.status_code == 400
    assert "tiny.en" in resp.json()["detail"]


def test_voice_transcription_saves_a_downloaded_model(authed_client, monkeypatch):
    monkeypatch.setattr("web_server.routes.config._vt_model_is_cached", lambda m: True)

    resp = authed_client.put(
        "/api/config/voice-transcription",
        json={"enabled": True, "model_name": "tiny.en", "channel_ids": ["4242"]},
    )
    assert resp.status_code == 200
    body = authed_client.get("/api/config").json()["voice_transcription"]
    assert body["model_name"] == "tiny.en"
    assert body["enabled"] is True


def test_voice_transcription_can_be_switched_off_with_its_model_gone(
    authed_client, monkeypatch
):
    """A cache that lost its model must not lock an admin out of their own
    settings — only *changing* to an uncached model is refused."""
    monkeypatch.setattr("web_server.routes.config._vt_model_is_cached", lambda m: True)
    authed_client.put(
        "/api/config/voice-transcription",
        json={"enabled": True, "model_name": "tiny.en", "channel_ids": []},
    )

    monkeypatch.setattr("web_server.routes.config._vt_model_is_cached", lambda m: False)
    resp = authed_client.put(
        "/api/config/voice-transcription",
        json={"enabled": False, "model_name": "tiny.en", "channel_ids": []},
    )
    assert resp.status_code == 200
