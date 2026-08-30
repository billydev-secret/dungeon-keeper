"""Tests for /api/wellness/admin/* — the wellness admin JSON API.

Covers snowflake-precision (ids must serialise as strings, never bare numbers
JS would round) and the pause/resume 404-on-no-match contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_service import (
    add_exempt_channel,
    opt_in_user,
)

# A snowflake larger than 2**53 — a bare JSON number here would lose precision.
BIG_USER = 1234567890123456789
BIG_CHANNEL = 8123456789012345678


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

BIG_ROLE = 7123456789012345678


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
