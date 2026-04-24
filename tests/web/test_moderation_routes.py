"""Tests for /api/moderation/* endpoints."""

from __future__ import annotations

from db_utils import open_db
from services.moderation import create_jail, create_ticket, create_warning


def _seed_jail(db_path, guild_id=123, user_id=1001, moderator_id=2001):
    with open_db(db_path) as conn:
        jail_id = create_jail(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason="test jail",
            stored_roles=[],
            channel_id=0,
            duration_seconds=3600,
        )
    return jail_id


def _seed_ticket(db_path, guild_id=123, user_id=1001):
    with open_db(db_path) as conn:
        ticket_id = create_ticket(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            channel_id=0,
            description="test ticket",
        )
    return ticket_id


def _seed_warning(db_path, guild_id=123, user_id=1001, moderator_id=2001):
    with open_db(db_path) as conn:
        warn_id = create_warning(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason="test warning",
        )
    return warn_id


# ── GET /api/moderation/jails ─────────────────────────────────────────

def test_jails_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/jails")
    assert resp.status_code == 200
    data = resp.json()
    assert "jails" in data
    assert data["jails"] == []


def test_jails_returns_seeded_jail(open_client, fake_ctx):
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/jails")
    assert resp.status_code == 200
    jails = resp.json()["jails"]
    assert len(jails) == 1
    assert jails[0]["user_id"] == str(1001)
    assert jails[0]["reason"] == "test jail"


# ── GET /api/moderation/tickets ───────────────────────────────────────

def test_tickets_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/tickets")
    assert resp.status_code == 200
    data = resp.json()
    assert "tickets" in data
    assert data["tickets"] == []


def test_tickets_returns_seeded_ticket(open_client, fake_ctx):
    _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/tickets")
    assert resp.status_code == 200
    tickets = resp.json()["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["user_id"] == str(1001)


# ── GET /api/moderation/warnings ─────────────────────────────────────

def test_warnings_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/warnings")
    assert resp.status_code == 200
    data = resp.json()
    assert "warnings" in data
    assert data["warnings"] == []


def test_warnings_returns_seeded_warning(open_client, fake_ctx):
    _seed_warning(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/warnings")
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["user_id"] == str(1001)
    assert warnings[0]["reason"] == "test warning"


# ── Auth guard ────────────────────────────────────────────────────────

def test_moderation_requires_auth(fake_ctx):
    """With Discord auth mode, moderation endpoints require a session."""
    from web.auth import DiscordOAuthAuth
    from web.server import create_app
    from fastapi.testclient import TestClient

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    # No session cookie set
    resp = client.get("/api/moderation/jails")
    assert resp.status_code in (401, 403)
    client.close()
