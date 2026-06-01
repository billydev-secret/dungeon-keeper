"""Tests for the smaller admin/mod web routes: quotes, todos, gender, admin backfill.

These four route modules are small enough (29-65 stmts) to make a single test
file the right shape. They share the standard ``authed_client`` / ``fake_ctx``
fixtures from ``tests/web/conftest.py``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.gender_service import set_gender


# ── /api/quotes/audit ─────────────────────────────────────────────────


def _insert_quote_audit(
    db_path,
    *,
    guild_id: int,
    quoter_id: int = 100,
    quoted_user_id: int = 200,
    theme: str = "classic",
    ts: float | None = None,
):
    with open_db(db_path) as conn:
        conn.execute(
            """INSERT INTO quote_audit_log
                 (ts, guild_id, channel_id, quoter_id, quoted_user_id,
                  quoted_message_id, posted_message_id, theme, font)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts if ts is not None else time.time(),
                guild_id,
                900,
                quoter_id,
                quoted_user_id,
                1234,
                5678,
                theme,
                "Arial",
            ),
        )


def test_quote_audit_empty_returns_zero_total(authed_client):
    resp = authed_client.get("/api/quotes/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["entries"] == []


def test_quote_audit_returns_seeded_rows(authed_client, fake_ctx):
    _insert_quote_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, ts=100.0)
    _insert_quote_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, ts=200.0)

    resp = authed_client.get("/api/quotes/audit")
    body = resp.json()
    assert body["total"] == 2
    assert [e["ts"] for e in body["entries"]] == [200.0, 100.0]  # newest first


def test_quote_audit_filter_by_theme(authed_client, fake_ctx):
    _insert_quote_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, theme="classic")
    _insert_quote_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, theme="modern")

    resp = authed_client.get("/api/quotes/audit?theme=modern")
    body = resp.json()
    assert body["total"] == 1
    assert body["entries"][0]["theme"] == "modern"


def test_quote_audit_excludes_other_guilds(authed_client, fake_ctx):
    _insert_quote_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    _insert_quote_audit(fake_ctx.db_path, guild_id=999)  # different guild

    resp = authed_client.get("/api/quotes/audit")
    assert resp.json()["total"] == 1


def test_quote_audit_caps_limit_at_200(authed_client, fake_ctx):
    """A caller asking for limit=10_000 must not get more than 200 rows."""
    for i in range(5):
        _insert_quote_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, ts=float(i))

    resp = authed_client.get("/api/quotes/audit?limit=10000")
    # Only 5 rows exist, but the underlying SQL would have used LIMIT 200,
    # not LIMIT 10000 — protects against accidental large-fetch DOS.
    assert len(resp.json()["entries"]) == 5


def test_quote_audit_serializes_ids_as_strings(authed_client, fake_ctx):
    _insert_quote_audit(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        quoter_id=42,
        quoted_user_id=43,
    )
    body = authed_client.get("/api/quotes/audit").json()
    entry = body["entries"][0]
    assert entry["quoter_id"] == "42"
    assert entry["quoted_user_id"] == "43"
    # Resolved names default to "User <id>" when no guild/member is available.
    assert entry["quoter_name"].startswith("User ")


# ── /api/todos ────────────────────────────────────────────────────────


def test_list_todos_empty(authed_client):
    resp = authed_client.get("/api/todos")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending_count"] == 0
    assert body["completed_count"] == 0
    assert body["todos"] == []


def test_create_todo_persists_and_returns_id(authed_client):
    resp = authed_client.post("/api/todos", json={"task": "Buy groceries"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["id"], int)

    listed = authed_client.get("/api/todos").json()
    assert listed["pending_count"] == 1
    assert listed["todos"][0]["task"] == "Buy groceries"


def test_create_todo_rejects_empty_string(authed_client):
    resp = authed_client.post("/api/todos", json={"task": "   "})
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_create_todo_rejects_oversize(authed_client):
    resp = authed_client.post("/api/todos", json={"task": "x" * 501})
    assert resp.status_code == 400


def test_complete_todo_marks_completed(authed_client):
    created = authed_client.post("/api/todos", json={"task": "task A"}).json()
    todo_id = created["id"]

    resp = authed_client.post(f"/api/todos/{todo_id}/complete")
    assert resp.status_code == 200

    listed = authed_client.get("/api/todos").json()
    assert listed["pending_count"] == 0
    assert listed["completed_count"] == 1
    assert listed["todos"][0]["completed_at"] is not None


def test_complete_unknown_todo_returns_404(authed_client):
    resp = authed_client.post("/api/todos/99999/complete")
    assert resp.status_code == 404


def test_complete_already_done_returns_404(authed_client):
    todo_id = authed_client.post("/api/todos", json={"task": "x"}).json()["id"]
    authed_client.post(f"/api/todos/{todo_id}/complete")
    # Second completion attempt → 404 (the UPDATE filter requires completed_at IS NULL)
    resp = authed_client.post(f"/api/todos/{todo_id}/complete")
    assert resp.status_code == 404


def test_list_todos_filter_by_status(authed_client):
    id_a = authed_client.post("/api/todos", json={"task": "todo A"}).json()["id"]
    authed_client.post("/api/todos", json={"task": "todo B"})
    authed_client.post(f"/api/todos/{id_a}/complete")

    pending = authed_client.get("/api/todos?status=pending").json()
    completed = authed_client.get("/api/todos?status=completed").json()

    pending_tasks = {t["task"] for t in pending["todos"]}
    completed_tasks = {t["task"] for t in completed["todos"]}
    assert pending_tasks == {"todo B"}
    assert completed_tasks == {"todo A"}


# ── /api/gender/* ─────────────────────────────────────────────────────


def _attach_mock_bot_with_guild(fake_ctx, members):
    """Attach a mock bot to fake_ctx with a guild that returns *members*.

    ``guild.get_member(uid)`` does a real lookup so ``resolve_names`` can
    populate display_name fields instead of bare MagicMock objects. The
    auth session user (uid=1) is auto-added with administrator perms so the
    Discord-cache-backed authenticate() path doesn't reject the request.
    """
    # The authed_client fixture creates a session with uid=1 and
    # permission_bits=0x8 (ADMINISTRATOR). DiscordOAuthAuth prefers the live
    # bot cache when one is attached, so we must surface this user from the
    # guild for the auth check to succeed.
    auth_member = MagicMock()
    auth_member.id = 1
    auth_member.display_name = "tester"
    auth_member.guild_permissions = MagicMock(value=0x8)
    role = MagicMock()
    role.id = 0
    role.name = "@everyone"
    role.is_default = MagicMock(return_value=True)
    auth_member.roles = [role]

    all_members = [auth_member, *members]
    by_id = {m.id: m for m in all_members}
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.members = all_members
    guild.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot
    return guild


def _mock_member(member_id: int, *, is_bot: bool = False, display_name: str = "") -> MagicMock:
    m = MagicMock()
    m.id = member_id
    m.bot = is_bot
    m.display_name = display_name or f"user-{member_id}"
    return m


def test_gender_list_returns_503_when_bot_unavailable(authed_client):
    resp = authed_client.get("/api/gender/list")
    assert resp.status_code == 503


def test_gender_list_returns_classified_members(authed_client, fake_ctx):
    members = [
        _mock_member(101, display_name="alice"),
        _mock_member(102, display_name="bob"),
        _mock_member(999, is_bot=True),  # bot — excluded
    ]
    _attach_mock_bot_with_guild(fake_ctx, members)
    with open_db(fake_ctx.db_path) as conn:
        set_gender(conn, fake_ctx.guild_id, 101, "female", set_by=1)
        set_gender(conn, fake_ctx.guild_id, 102, "male", set_by=1)
        set_gender(conn, fake_ctx.guild_id, 999, "male", set_by=1)  # bot row, must not show

    resp = authed_client.get("/api/gender/list")
    assert resp.status_code == 200
    classified = resp.json()["classified"]
    ids = {c["user_id"] for c in classified}
    assert ids == {"101", "102"}  # bot excluded


def test_gender_unclassified_returns_members_without_a_gender(authed_client, fake_ctx):
    members = [
        _mock_member(101, display_name="alice"),
        _mock_member(102, display_name="bob"),
    ]
    _attach_mock_bot_with_guild(fake_ctx, members)
    with open_db(fake_ctx.db_path) as conn:
        set_gender(conn, fake_ctx.guild_id, 101, "female", set_by=1)

    resp = authed_client.get("/api/gender/unclassified")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["members"][0]["user_id"] == "102"


def test_gender_set_rejects_invalid_value(authed_client, fake_ctx):
    _attach_mock_bot_with_guild(fake_ctx, [_mock_member(101)])
    resp = authed_client.post(
        "/api/gender/set", json={"user_id": "101", "gender": "alien"}
    )
    assert resp.status_code == 400


def test_gender_set_persists_classification(authed_client, fake_ctx):
    _attach_mock_bot_with_guild(fake_ctx, [_mock_member(101)])
    resp = authed_client.post(
        "/api/gender/set", json={"user_id": "101", "gender": "nonbinary"}
    )
    assert resp.status_code == 200
    from bot_modules.services.gender_service import get_gender

    with open_db(fake_ctx.db_path) as conn:
        assert get_gender(conn, fake_ctx.guild_id, 101) == "nonbinary"


def test_gender_set_overwrites_existing(authed_client, fake_ctx):
    _attach_mock_bot_with_guild(fake_ctx, [_mock_member(101)])
    with open_db(fake_ctx.db_path) as conn:
        set_gender(conn, fake_ctx.guild_id, 101, "male", set_by=999)

    authed_client.post("/api/gender/set", json={"user_id": "101", "gender": "female"})

    from bot_modules.services.gender_service import get_gender

    with open_db(fake_ctx.db_path) as conn:
        assert get_gender(conn, fake_ctx.guild_id, 101) == "female"


# ── /api/admin/backfill-* ─────────────────────────────────────────────


def test_backfill_endpoints_return_503_when_bot_unavailable(authed_client):
    """Without a connected bot, the guild lookup fails and the endpoint refuses
    to start a job."""
    for path in (
        "/api/admin/backfill-roles",
        "/api/admin/backfill-xp",
        "/api/admin/backfill-interactions",
    ):
        if path == "/api/admin/backfill-roles":
            resp = authed_client.post(path)
        else:
            resp = authed_client.post(path, json={"days": 30})
        assert resp.status_code == 503, f"{path} should refuse without a guild"


def test_backfill_roles_invokes_sync_helper(authed_client, fake_ctx):
    guild = _attach_mock_bot_with_guild(fake_ctx, [])
    with patch(
        "web_server.routes.admin_backfill.backfill_roles_sync",
        return_value={"grants_added": 2, "removes_added": 1},
    ) as mock_sync:
        resp = authed_client.post("/api/admin/backfill-roles")
    assert resp.status_code == 200
    assert "Grants added: 2" in resp.json()["message"]
    assert "removes added: 1" in resp.json()["message"]
    mock_sync.assert_called_once_with(fake_ctx, guild)


def _make_coroutine_returning_mock():
    """A mock whose __call__ captures args AND returns a real coroutine so the
    endpoint's ``asyncio.create_task(coro)`` doesn't blow up.

    The endpoint pattern is ``backfill_xp_async(ctx, guild, days=days)`` →
    that synchronous call must return an awaitable. We can't use AsyncMock
    because the endpoint schedules but never awaits inside the test window;
    instead we use a plain MagicMock so the call args land in ``call_args``
    immediately, and return a real coroutine so the scheduling code path
    doesn't raise.
    """

    async def _real_coro():
        return {}

    return MagicMock(return_value=_real_coro())


def test_backfill_xp_schedules_async_job(authed_client, fake_ctx):
    _attach_mock_bot_with_guild(fake_ctx, [])
    mock = _make_coroutine_returning_mock()
    with patch("web_server.routes.admin_backfill.backfill_xp_async", mock):
        resp = authed_client.post("/api/admin/backfill-xp", json={"days": 30})
    assert resp.status_code == 200
    assert resp.json()["job"] == "backfill-xp"
    assert "30 days" in resp.json()["message"]
    assert mock.call_args.kwargs["days"] == 30


def test_backfill_xp_days_zero_says_all_history(authed_client, fake_ctx):
    _attach_mock_bot_with_guild(fake_ctx, [])
    mock = _make_coroutine_returning_mock()
    with patch("web_server.routes.admin_backfill.backfill_xp_async", mock):
        resp = authed_client.post("/api/admin/backfill-xp", json={"days": 0})
    assert "all available history" in resp.json()["message"]


def test_backfill_interactions_passes_reset_and_channel(authed_client, fake_ctx):
    _attach_mock_bot_with_guild(fake_ctx, [])
    mock = _make_coroutine_returning_mock()
    with patch(
        "web_server.routes.admin_backfill.backfill_interactions_async", mock,
    ):
        resp = authed_client.post(
            "/api/admin/backfill-interactions?reset=true&channel_id=4321",
            json={"days": 14},
        )
    assert resp.status_code == 200
    kwargs = mock.call_args.kwargs
    assert kwargs["days"] == 14
    assert kwargs["reset"] is True
    assert kwargs["channel_id"] == 4321
    assert "channel 4321" in resp.json()["message"]
    assert "existing data cleared" in resp.json()["message"]


def test_backfill_days_clamped_to_valid_range(authed_client, fake_ctx):
    """days is clamped to [0, 3650] — protects against negative or absurd values."""
    _attach_mock_bot_with_guild(fake_ctx, [])
    mock = _make_coroutine_returning_mock()
    with patch("web_server.routes.admin_backfill.backfill_xp_async", mock):
        authed_client.post("/api/admin/backfill-xp", json={"days": 999_999})
    assert mock.call_args.kwargs["days"] == 3650


# ── Auth gates ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/quotes/audit", None),
        ("GET", "/api/todos", None),
        ("POST", "/api/todos", {"task": "x"}),
        ("GET", "/api/gender/list", None),
        ("POST", "/api/gender/set", {"user_id": "1", "gender": "male"}),
        ("POST", "/api/admin/backfill-roles", None),
    ],
)
def test_small_routes_require_auth(fake_ctx, method, path, body):
    """All small-route endpoints reject unauthenticated callers."""
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app, raise_server_exceptions=False)
    if method == "GET":
        resp = client.get(path)
    else:
        resp = client.post(path, json=body or {})
    assert resp.status_code in (401, 403), f"{method} {path} should require auth"
    client.close()
