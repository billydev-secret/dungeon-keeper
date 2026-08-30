"""Tests for /api/wellness/admin/* — the wellness admin JSON API.

Covers snowflake-precision (ids must serialise as strings, never bare numbers
JS would round), the pause/resume 404-on-no-match contract, and the
provisioning routes that write the `role_id`/`channel_id` keys gating the
whole feature (docs/plans/wellness-relaunch.md Stage D).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_service import (
    add_exempt_channel,
    get_wellness_config,
    opt_in_user,
)

# A snowflake larger than 2**53 — a bare JSON number here would lose precision.
BIG_USER = 1234567890123456789
BIG_CHANNEL = 8123456789012345678
BIG_ROLE = 9123456789012345678


def _opt_in(fake_ctx, user_id: int, *, timezone: str = "UTC"):
    with open_db(fake_ctx.db_path) as conn:
        return opt_in_user(conn, fake_ctx.guild_id, user_id, timezone=timezone)


# ── snowflake precision ──────────────────────────────────────────────


def test_admin_users_stringifies_user_id(authed_client, fake_ctx):
    _opt_in(fake_ctx, BIG_USER)
    body = authed_client.get("/api/wellness/admin/users").json()
    assert len(body["users"]) == 1
    assert body["users"][0]["user_id"] == str(BIG_USER)


def test_admin_exempt_stringifies_channel_id(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        add_exempt_channel(conn, fake_ctx.guild_id, BIG_CHANNEL, "#big")
    body = authed_client.get("/api/wellness/admin/exempt").json()
    assert len(body["exempt"]) == 1
    assert body["exempt"][0]["id"] == str(BIG_CHANNEL)


def test_admin_exempt_stringifies_channel_option_ids(authed_client, fake_ctx):
    ch = MagicMock()
    ch.id = BIG_CHANNEL
    ch.name = "general"
    guild = MagicMock()
    guild.text_channels = [ch]
    guild.get_channel = MagicMock(return_value=None)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    body = authed_client.get("/api/wellness/admin/exempt").json()
    assert body["channel_options"][0]["id"] == str(BIG_CHANNEL)


def test_admin_dashboard_stringifies_exempt_ids(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        add_exempt_channel(conn, fake_ctx.guild_id, BIG_CHANNEL, "#big")
    body = authed_client.get("/api/wellness/admin/dashboard").json()
    assert body["exempt_channels"][0]["id"] == str(BIG_CHANNEL)


# ── pause / resume 404-on-no-match ───────────────────────────────────


def test_admin_pause_unknown_user_returns_404(authed_client, fake_ctx):
    # No wellness_users row for this id — the UPDATE matches zero rows, so the
    # panel must not be told the pause succeeded.
    resp = authed_client.post(
        "/api/wellness/admin/users/424242/pause", json={"minutes": 30}
    )
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_admin_resume_unknown_user_returns_404(authed_client, fake_ctx):
    resp = authed_client.post("/api/wellness/admin/users/424242/resume")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_admin_pause_and_resume_known_user_ok(authed_client, fake_ctx):
    _opt_in(fake_ctx, BIG_USER)
    paused = authed_client.post(
        f"/api/wellness/admin/users/{BIG_USER}/pause", json={"minutes": 30}
    )
    assert paused.json()["ok"] is True
    resumed = authed_client.post(f"/api/wellness/admin/users/{BIG_USER}/resume")
    assert resumed.json()["ok"] is True


# ── the activation pair: opt-in role + announcement channel ──────────
#
# `/wellness setup` refuses with "An admin can configure it from the web
# dashboard" until role_id is set, and the scheduler skips the active list and
# the milestone posts until channel_id is. Nothing in the code ever wrote
# either, so that message pointed at a control that did not exist and a fresh
# guild could never switch the programme on.

def test_admin_defaults_saves_the_role_and_channel(authed_client, fake_ctx):
    from bot_modules.services.wellness_service import get_wellness_config

    resp = authed_client.post(
        "/api/wellness/admin/defaults",
        json={
            "default_enforcement": "gentle",
            "role_id": str(BIG_ROLE),
            "channel_id": str(BIG_CHANNEL),
        },
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    with open_db(fake_ctx.db_path) as conn:
        cfg = get_wellness_config(conn, fake_ctx.guild_id)
    assert cfg is not None
    assert cfg.role_id == BIG_ROLE
    assert cfg.channel_id == BIG_CHANNEL
    assert cfg.default_enforcement == "gentle"


def test_admin_defaults_reports_the_role_and_channel_as_strings(
    authed_client, fake_ctx
):
    authed_client.post(
        "/api/wellness/admin/defaults",
        json={"role_id": str(BIG_ROLE), "channel_id": str(BIG_CHANNEL)},
    )
    cfg = authed_client.get("/api/wellness/admin/defaults").json()["config"]
    assert cfg["role_id"] == str(BIG_ROLE)
    assert cfg["channel_id"] == str(BIG_CHANNEL)


def test_admin_defaults_clears_the_pair_with_zero(authed_client, fake_ctx):
    """0 is "not set" everywhere the gates read these, so it must be storable —
    an admin has to be able to take the programme back off the air."""
    from bot_modules.services.wellness_service import get_wellness_config

    authed_client.post(
        "/api/wellness/admin/defaults",
        json={"role_id": str(BIG_ROLE), "channel_id": str(BIG_CHANNEL)},
    )
    authed_client.post(
        "/api/wellness/admin/defaults", json={"role_id": "0", "channel_id": "0"}
    )
    with open_db(fake_ctx.db_path) as conn:
        cfg = get_wellness_config(conn, fake_ctx.guild_id)
    assert cfg is not None and cfg.role_id == 0 and cfg.channel_id == 0


def test_admin_defaults_rejects_a_non_numeric_id(authed_client):
    resp = authed_client.post(
        "/api/wellness/admin/defaults", json={"role_id": "not-a-snowflake"}
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
# ── provisioning (role_id / channel_id — the keys that gate the feature) ─


def _mk_role(rid: int, name: str, *, managed: bool = False) -> MagicMock:
    role = MagicMock()
    role.id = rid
    role.name = name
    role.managed = managed
    return role


def _mk_channel(cid: int, name: str) -> MagicMock:
    ch = MagicMock()
    ch.id = cid
    ch.name = name
    return ch


def _wire_guild(fake_ctx, *, roles=(), channels=()) -> MagicMock:
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.roles = list(roles)
    guild.text_channels = list(channels)
    guild.get_role = lambda rid: next(
        (r for r in guild.roles if r.id == rid), None
    )
    guild.get_channel = lambda cid: next(
        (c for c in guild.text_channels if c.id == cid), None
    )
    guild.create_role = AsyncMock()
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot
    return guild


def _stored_config(fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        return get_wellness_config(conn, fake_ctx.guild_id)


def test_provision_get_without_bot_reports_disconnected(authed_client, fake_ctx):
    body = authed_client.get("/api/wellness/admin/provision").json()
    assert body["bot_connected"] is False
    assert body["role_id"] == "0"
    assert body["role_name"] is None
    assert body["role_options"] == []


def test_provision_get_stringifies_option_ids(authed_client, fake_ctx):
    # @everyone (id == guild.id) and managed roles can't be handed out, so
    # neither may appear as an option.
    _wire_guild(
        fake_ctx,
        roles=[
            _mk_role(fake_ctx.guild_id, "@everyone"),
            _mk_role(BIG_ROLE, "Wellness Guardian"),
            _mk_role(4242, "SomeBot", managed=True),
        ],
        channels=[_mk_channel(BIG_CHANNEL, "wellness")],
    )
    body = authed_client.get("/api/wellness/admin/provision").json()
    assert body["bot_connected"] is True
    assert [r["id"] for r in body["role_options"]] == [str(BIG_ROLE)]
    assert [c["id"] for c in body["channel_options"]] == [str(BIG_CHANNEL)]


def test_provision_get_resolves_stored_ids(authed_client, fake_ctx):
    _wire_guild(
        fake_ctx,
        roles=[_mk_role(BIG_ROLE, "Wellness Guardian")],
        channels=[_mk_channel(BIG_CHANNEL, "wellness")],
    )
    authed_client.post(
        "/api/wellness/admin/provision/role", json={"role_id": str(BIG_ROLE)}
    )
    authed_client.post(
        "/api/wellness/admin/provision/channel",
        json={"channel_id": str(BIG_CHANNEL)},
    )
    body = authed_client.get("/api/wellness/admin/provision").json()
    assert body["role_id"] == str(BIG_ROLE)
    assert body["role_name"] == "Wellness Guardian"
    assert body["channel_id"] == str(BIG_CHANNEL)
    assert body["channel_name"] == "wellness"


def test_provision_role_pick_existing_stores_id(authed_client, fake_ctx):
    _wire_guild(fake_ctx, roles=[_mk_role(BIG_ROLE, "Zen")])
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"role_id": str(BIG_ROLE)}
    )
    assert resp.json()["ok"] is True
    assert resp.json()["role_id"] == str(BIG_ROLE)
    assert _stored_config(fake_ctx).role_id == BIG_ROLE


def test_provision_role_rejects_unknown_role(authed_client, fake_ctx):
    _wire_guild(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"role_id": 999}
    )
    assert resp.status_code == 404
    assert _stored_config(fake_ctx) is None


def test_provision_role_rejects_managed_role(authed_client, fake_ctx):
    _wire_guild(fake_ctx, roles=[_mk_role(555, "SomeBot", managed=True)])
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"role_id": 555}
    )
    assert resp.status_code == 400
    assert _stored_config(fake_ctx) is None


def test_provision_role_rejects_everyone(authed_client, fake_ctx):
    guild = _wire_guild(fake_ctx)
    guild.roles = [_mk_role(guild.id, "@everyone")]
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"role_id": guild.id}
    )
    assert resp.status_code == 400


def test_provision_role_requires_bot(authed_client, fake_ctx):
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"role_id": 1}
    )
    assert resp.status_code == 503


def test_provision_role_auto_create_creates_and_stores(authed_client, fake_ctx):
    guild = _wire_guild(fake_ctx)
    guild.create_role.return_value = _mk_role(BIG_ROLE, "Wellness Guardian")
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"auto_create": True}
    )
    assert resp.json()["ok"] is True
    assert resp.json()["role_id"] == str(BIG_ROLE)
    assert _stored_config(fake_ctx).role_id == BIG_ROLE
    guild.create_role.assert_awaited_once()


def test_provision_role_auto_create_adopts_existing_name(authed_client, fake_ctx):
    # A guild that already has a "Wellness Guardian" role keeps it — the
    # adopt-by-name step must win over creating a twin.
    guild = _wire_guild(fake_ctx, roles=[_mk_role(BIG_ROLE, "Wellness Guardian")])
    resp = authed_client.post(
        "/api/wellness/admin/provision/role", json={"auto_create": True}
    )
    assert resp.json()["ok"] is True
    assert resp.json()["role_id"] == str(BIG_ROLE)
    assert _stored_config(fake_ctx).role_id == BIG_ROLE
    guild.create_role.assert_not_awaited()


def test_provision_channel_stores_id(authed_client, fake_ctx):
    _wire_guild(fake_ctx, channels=[_mk_channel(BIG_CHANNEL, "wellness")])
    resp = authed_client.post(
        "/api/wellness/admin/provision/channel",
        json={"channel_id": str(BIG_CHANNEL)},
    )
    assert resp.json()["ok"] is True
    assert resp.json()["channel_id"] == str(BIG_CHANNEL)
    assert _stored_config(fake_ctx).channel_id == BIG_CHANNEL


def test_provision_channel_rejects_unknown_channel(authed_client, fake_ctx):
    _wire_guild(fake_ctx)
    resp = authed_client.post(
        "/api/wellness/admin/provision/channel", json={"channel_id": 999}
    )
    assert resp.status_code == 404
    assert _stored_config(fake_ctx) is None


def test_provision_channel_requires_bot(authed_client, fake_ctx):
    resp = authed_client.post(
        "/api/wellness/admin/provision/channel", json={"channel_id": 1}
    )
    assert resp.status_code == 503
