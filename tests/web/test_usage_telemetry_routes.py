"""Usage telemetry routes — panel-view ingest and the /api/reports/usage report."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import usage_telemetry_service as svc

sys.path.insert(0, str(Path(__file__).resolve().parent))

GUILD = 123


def _seed(db_path, **kw):
    with open_db(db_path) as conn:
        svc.record_event(conn, GUILD, **kw)


# ── panel-view ingest ────────────────────────────────────────────────────


def test_panel_view_recorded(authed_client, web_db):
    r = authed_client.post("/api/telemetry/panel", json={"panel": "economy-stats"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with open_db(web_db) as conn:
        row = conn.execute(
            "SELECT kind, name, user_id FROM usage_events"
        ).fetchone()
    assert row[0] == svc.KIND_PANEL
    assert row[1] == "economy-stats"
    # The session cookie's user, not anything the client claimed.
    assert row[2] == 1


def test_panel_view_rejects_empty_name(authed_client):
    r = authed_client.post("/api/telemetry/panel", json={"panel": ""})
    assert r.status_code == 422


def test_panel_view_rejects_overlong_name(authed_client):
    r = authed_client.post("/api/telemetry/panel", json={"panel": "x" * 500})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "panel",
    ["Economy Stats", "econ/stats", "../etc", "-leading-dash", "UPPER", "a b"],
)
def test_panel_view_rejects_non_panel_id_shapes(authed_client, panel):
    """Only kebab-case route ids get in — keeps junk out of the name column."""
    assert authed_client.post(
        "/api/telemetry/panel", json={"panel": panel}
    ).status_code == 422


def _client_with_perms(fake_ctx, permission_bits: int, user_id: int):
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
    from web_server.server import create_app

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth))
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=user_id,
            username="u",
            access_token="token",
            permission_bits=permission_bits,
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
        ),
    )
    return client


def test_panel_view_rejects_plain_members(fake_ctx, web_db):
    """A member with no mod perms must not be able to write panel views — they
    could otherwise make a never-opened panel look used, which is the one
    number on this report that has to be right."""
    client = _client_with_perms(fake_ctx, 0, user_id=4242)
    r = client.post("/api/telemetry/panel", json={"panel": "quality-score"})
    assert r.status_code == 403
    with open_db(web_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    client.close()


def test_panel_view_accepts_moderators(fake_ctx, web_db):
    client = _client_with_perms(fake_ctx, 0x2000, user_id=4243)  # manage_messages
    assert client.post(
        "/api/telemetry/panel", json={"panel": "mod-tickets"}
    ).status_code == 200
    with open_db(web_db) as conn:
        assert conn.execute("SELECT name FROM usage_events").fetchone()[0] == "mod-tickets"
    client.close()


# ── report ───────────────────────────────────────────────────────────────


def test_usage_report_shape(authed_client, web_db):
    _seed(web_db, kind=svc.KIND_COMMAND, name="bank", user_id=1001)
    _seed(web_db, kind=svc.KIND_COMMAND, name="bank", user_id=1002, ok=False)
    _seed(web_db, kind=svc.KIND_PANEL, name="home", user_id=1001)

    r = authed_client.get("/api/reports/usage", params={"days": 30})
    assert r.status_code == 200
    body = r.json()

    assert body["totals"] == {
        "commands": 2,
        "panel_views": 1,
        "command_errors": 1,
        "distinct_users": 2,
    }
    assert [c["name"] for c in body["commands"]] == ["bank"]
    assert body["commands"][0]["errors"] == 1
    assert [p["name"] for p in body["panels"]] == ["home"]
    assert len(body["daily_commands"]) == 30
    assert len(body["hours"]) == 24


def test_usage_report_stringifies_snowflakes(authed_client, web_db):
    """A Discord id must never cross the wire as a bare JSON number."""
    big = 1469491362444480666
    assert big > 2**53
    _seed(web_db, kind=svc.KIND_COMMAND, name="bank", user_id=big)

    body = authed_client.get("/api/reports/usage").json()
    assert body["top_users"][0]["user_id"] == str(big)
    assert isinstance(body["top_users"][0]["user_id"], str)


def test_seen_panels_returned_for_client_side_diff(authed_client, web_db):
    """The full nav list is too big for a query param, so the server returns
    what it has seen and the client subtracts."""
    _seed(web_db, kind=svc.KIND_PANEL, name="home", user_id=1001)
    _seed(web_db, kind=svc.KIND_PANEL, name="home", user_id=1002)
    _seed(web_db, kind=svc.KIND_PANEL, name="economy-stats", user_id=1001)

    body = authed_client.get("/api/reports/usage").json()
    assert body["seen_panels"] == ["economy-stats", "home"]


def test_seen_panels_spans_all_history_not_the_window(authed_client, web_db):
    """A panel opened once a year ago is not 'never opened'."""
    with open_db(web_db) as conn:
        svc.record_event(
            conn, GUILD, svc.KIND_PANEL, "home", 1001,
            ts=time.time() - 400 * 86400,
        )
    body = authed_client.get("/api/reports/usage", params={"days": 7}).json()
    assert body["seen_panels"] == ["home"]


def test_unused_commands_empty_in_standalone_mode(authed_client, web_db):
    """No bot attached → no command tree → claim nothing is unused, rather
    than claiming every command is."""
    _seed(web_db, kind=svc.KIND_COMMAND, name="bank", user_id=1001)
    body = authed_client.get("/api/reports/usage").json()
    assert body["unused_commands"] == []


def test_unused_commands_excludes_command_groups(authed_client, fake_ctx, web_db):
    """A Group like /quest can't be invoked, so it must never be listed as
    'never run' — that list has to stay a straight list of deletable things."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from discord import app_commands

    group = MagicMock(spec=app_commands.Group)
    group.qualified_name = "quest"
    leaf = MagicMock(spec=app_commands.Command)
    leaf.qualified_name = "quest board"
    orphan = MagicMock(spec=app_commands.Command)
    orphan.qualified_name = "bank"

    tree = SimpleNamespace(walk_commands=lambda: [group, leaf, orphan])
    fake_ctx.bot = SimpleNamespace(tree=tree, get_guild=lambda _gid: None)

    _seed(web_db, kind=svc.KIND_COMMAND, name="quest board", user_id=1001)

    body = authed_client.get("/api/reports/usage").json()
    # "bank" is genuinely unrun; "quest" is a group and must not appear.
    assert body["unused_commands"] == ["bank"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 1), (-5, 1), (9999, 365), (7, 7)],
)
def test_days_is_clamped(authed_client, requested, expected):
    body = authed_client.get("/api/reports/usage", params={"days": requested}).json()
    assert body["days"] == expected


def test_report_requires_admin(fake_ctx):
    """Moderator-level sessions must not see per-member usage."""
    client = _client_with_perms(fake_ctx, 0x2000, user_id=2)  # manage_messages
    assert client.get("/api/reports/usage").status_code == 403
    client.close()
