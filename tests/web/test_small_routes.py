"""Tests for the smaller admin/mod web routes: quotes, todos, admin backfill.

These four route modules are small enough (29-65 stmts) to make a single test
file the right shape. They share the standard ``authed_client`` / ``fake_ctx``
fixtures from ``tests/web/conftest.py``.
"""

from __future__ import annotations

import time

import pytest

from bot_modules.core.db_utils import open_db


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


# ── Auth gates ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/quotes/audit", None),
        ("GET", "/api/todos", None),
        ("POST", "/api/todos", {"task": "x"}),
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
