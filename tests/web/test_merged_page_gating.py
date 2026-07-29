"""Merged report+settings pages: the server is the enforcement, not the UI.

Pages like XP & Leveling now put a moderator-level report and its admin-level
settings on one pane, with the settings half rendered read-only for non-admins
(``lockUnlessAdmin`` in static/js/config-helpers.js). That lock is a courtesy —
it stops a moderator filling in a form whose save could never land. These tests
pin the half that actually matters:

* ``GET /api/config`` stays readable by a moderator, which is *why* the merged
  page can show real values instead of blanking them. If this ever tightened to
  admin, the settings section would render empty for the audience it was merged
  for, and the merge would be pointless.
* Config writes stay refused for a moderator, so stripping ``disabled`` in
  devtools buys nothing.

Written against the routes rather than the browser because the browser suite
runs unauthenticated-as-admin; asserting the lock's *appearance* there would
prove less than asserting the permission boundary here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web_server.server import create_app
from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE

# Discord permission bits, per web_server.auth._perms_from_bits.
_ADMINISTRATOR = 0x8
_MANAGE_MESSAGES = 0x2000  # a moderator bit that does NOT imply admin


def _client(fake_ctx, permission_bits: int) -> TestClient:
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth))
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=1,
            username="tester",
            access_token="token",
            permission_bits=permission_bits,
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
        ),
    )
    return client


def test_moderator_is_not_admin(fake_ctx):
    """Guard the fixture itself: MANAGE_MESSAGES must not confer admin.

    If this bit ever implied admin, every assertion below would pass for the
    wrong reason.
    """
    resp = _client(fake_ctx, _MANAGE_MESSAGES).get("/api/me")
    assert resp.status_code == 200
    perms = set(resp.json()["perms"])
    assert "moderator" in perms
    assert "admin" not in perms


def test_moderator_can_read_config(fake_ctx):
    """The merged page shows real values to a moderator, not a blank form."""
    resp = _client(fake_ctx, _MANAGE_MESSAGES).get("/api/config")
    assert resp.status_code == 200
    assert "xp" in resp.json()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/config/xp", {"level_curve_factor": 99.0}),
        ("/api/config/prune", {"role_id": "0", "inactivity_days": 5}),
        ("/api/config/bulk-cleanup", {"enabled": True, "age_days": 1}),
    ],
)
def test_moderator_cannot_write_config(fake_ctx, path, payload):
    """The read-only lock is cosmetic; the write is refused server-side."""
    resp = _client(fake_ctx, _MANAGE_MESSAGES).put(path, json=payload)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/config/xp", {"level_curve_factor": 99.0}),
        ("/api/config/prune", {"role_id": "0", "inactivity_days": 5}),
    ],
)
def test_admin_can_still_write_config(fake_ctx, path, payload):
    """The counterpart — gating a section must not break the admin path."""
    resp = _client(fake_ctx, _ADMINISTRATOR).put(path, json=payload)
    assert resp.status_code == 200


# ── Pen Pals: a deliberate widening, not drift ──────────────────────────────
#
# Pen Pals is the one merged page with no in-page lock, because its settings
# writes were lowered from admin to moderator so both halves sit at one level.
# That is a real permission change to production, so it gets its own assertions
# rather than riding on the generic "moderators can't write config" rule above —
# if it ever silently reverts to admin, a moderator loses a page they now own.


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/config/pen-pals", {"category_id": "0"}),
        (
            "/api/config/pen-pals/timers",
            {
                "session_seconds": 86400,
                "match_cooldown_seconds": 2592000,
                "max_question_swaps": 3,
                "warn_seconds": 3600,
                "question_suppress_seconds": 3600,
            },
        ),
        ("/api/config/pen-pals/separations", {"separations": []}),
    ],
)
def test_moderator_can_write_pen_pals_config(fake_ctx, path, payload):
    """Pen Pals settings are moderator-level; the other config writes are not."""
    resp = _client(fake_ctx, _MANAGE_MESSAGES).put(path, json=payload)
    assert resp.status_code == 200, resp.text


def test_pen_pals_widening_did_not_leak_to_its_neighbours(fake_ctx):
    """Lowering Pen Pals must not have lowered the module's other writes.

    Same file, adjacent routes — the failure mode is a copied decorator, so
    assert a neighbour still refuses the same moderator session.
    """
    client = _client(fake_ctx, _MANAGE_MESSAGES)
    assert client.put("/api/config/pen-pals", json={"category_id": "0"}).status_code == 200
    assert client.put("/api/config/xp", json={"level_curve_factor": 99.0}).status_code == 403
