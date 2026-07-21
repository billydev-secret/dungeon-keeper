"""Tests for /api/reports/* endpoints.

Most tests are shape-only: seed minimal data, call the endpoint, assert the
response has the expected top-level keys and a non-error status.  Heavy
computation endpoints get a smoke-only test (200 + valid JSON).
"""

from __future__ import annotations

import time

import pytest

from bot_modules.core.db_utils import open_db
from web_server.deps import invalidate_report_cache


def _seed_messages(db_path, guild_id=123, count=5):
    """Insert minimal message rows for data-presence tests."""
    with open_db(db_path) as conn:
        for i in range(count):
            conn.execute(
                """INSERT OR IGNORE INTO messages
                   (message_id, guild_id, channel_id, author_id, ts, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (1000 + i, guild_id, 2001, 3001 + i, int(time.time()) - i * 3600, "hello world"),
            )
        conn.commit()


def _seed_xp(db_path, guild_id=123):
    with open_db(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO member_xp (guild_id, user_id, total_xp, level)
               VALUES (?, ?, ?, ?)""",
            (guild_id, 3001, 500, 2),
        )
        conn.commit()


# ── role-growth ───────────────────────────────────────────────────────


def test_role_growth_shape(open_client):
    resp = open_client.get("/api/reports/role-growth")
    assert resp.status_code == 200
    data = resp.json()
    assert "labels" in data
    assert "series" in data
    assert "resolution" in data


# ── message-cadence ───────────────────────────────────────────────────


def test_message_cadence_shape(open_client):
    resp = open_client.get("/api/reports/message-cadence")
    assert resp.status_code == 200
    data = resp.json()
    assert "buckets" in data
    assert "resolution" in data


def test_message_cadence_with_data(open_client, fake_ctx):
    invalidate_report_cache()
    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id)
    resp = open_client.get("/api/reports/message-cadence")
    assert resp.status_code == 200


# ── message-rate ──────────────────────────────────────────────────────


def test_message_rate_shape(open_client):
    resp = open_client.get("/api/reports/message-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert "buckets" in data
    assert "avg_per_day" in data


# ── xp-leaderboard ────────────────────────────────────────────────────


def test_xp_leaderboard_empty(open_client):
    resp = open_client.get("/api/reports/xp-leaderboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "leaderboard" in data
    assert data["leaderboard"] == []


def test_xp_leaderboard_with_data(open_client, fake_ctx):
    invalidate_report_cache()
    _seed_xp(fake_ctx.db_path, fake_ctx.guild_id)
    resp = open_client.get("/api/reports/xp-leaderboard")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["leaderboard"]) == 1
    assert data["leaderboard"][0]["total_xp"] == 500


# ── activity ──────────────────────────────────────────────────────────


def test_activity_shape(open_client):
    resp = open_client.get("/api/reports/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert "labels" in data
    assert "counts" in data
    assert "resolution" in data


# ── quality-score ─────────────────────────────────────────────────────


def test_quality_score_shape(open_client):
    resp = open_client.get("/api/reports/quality-score")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data
    assert "total_scored" in data


# ── heavy endpoints: smoke only ───────────────────────────────────────


_SMOKE_ENDPOINTS = [
    "/api/reports/interaction-graph",
    "/api/reports/interaction-heatmap",
]


@pytest.mark.parametrize("path", _SMOKE_ENDPOINTS)
def test_heavy_report_returns_200(open_client, path):
    resp = open_client.get(path)
    assert resp.status_code == 200
    assert resp.json() is not None


# ── Auth guard ────────────────────────────────────────────────────────


def test_reports_require_auth(fake_ctx):
    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app
    from fastapi.testclient import TestClient
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/reports/role-growth")
    assert resp.status_code in (401, 403)
    client.close()


# ── grant-audit ───────────────────────────────────────────────────────


class _FakeRole:
    def __init__(self, role_id, members=()):
        self.id = role_id
        self.name = "NSFW"
        self.members = list(members)


class _FakeMember:
    def __init__(self, user_id, display_name="", bot=False, roles=()):
        self.id = user_id
        self.display_name = display_name or str(user_id)
        self.bot = bot
        self.roles = list(roles)


class _FakeGuild:
    def __init__(self, role, members):
        self._role = role
        self._members = {m.id: m for m in members}

    def get_role(self, role_id):
        return self._role if role_id == self._role.id else None

    def get_member(self, user_id):
        return self._members.get(user_id)


class _FakeBot:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild


def _seed_grant_audit(db_path, guild_id, role_id):
    now = time.time()
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO grant_roles (guild_id, grant_name, label, role_id) "
            "VALUES (?, 'nsfw', 'NSFW', ?)",
            (guild_id, role_id),
        )
        # 3001: level 6, never granted, never pruned → waiting bucket.
        # 3002: pruned 5d ago, active again yesterday → stripped-returned.
        # 3003: pruned 3d ago, last active 60d ago → recent-inactive.
        for uid, level in ((3001, 6), (3002, 7)):
            conn.execute(
                "INSERT INTO member_xp (guild_id, user_id, total_xp, level) "
                "VALUES (?, ?, 0, ?)",
                (guild_id, uid, level),
            )
        for uid, pruned_days in ((3002, 5), (3003, 3)):
            conn.execute(
                "INSERT INTO role_prune_events (guild_id, user_id, role_id, pruned_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, uid, role_id, now - pruned_days * 86400),
            )
        for uid, active_days in ((3001, 1), (3002, 1), (3003, 60)):
            conn.execute(
                "INSERT INTO member_activity (guild_id, user_id, last_channel_id, "
                "last_message_id, last_message_at) VALUES (?, ?, 1, 1, ?)",
                (guild_id, uid, now - active_days * 86400),
            )
        conn.commit()


def test_grant_audit_buckets(open_client, fake_ctx):
    role_id = 555
    _seed_grant_audit(fake_ctx.db_path, fake_ctx.guild_id, role_id)
    role = _FakeRole(role_id)
    guild = _FakeGuild(role, [_FakeMember(uid) for uid in (3001, 3002, 3003)])
    fake_ctx.bot = _FakeBot(guild)

    resp = open_client.get("/api/reports/grant-audit?grant_name=nsfw&min_level=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["label"] == "NSFW"
    assert data["role_id"] == str(role_id)
    assert [r["user_id"] for r in data["waiting_first_grant"]] == ["3001"]
    assert [r["user_id"] for r in data["stripped_returned"]] == ["3002"]
    assert data["stripped_returned"][0]["level"] == 7
    assert [r["user_id"] for r in data["recent_inactive"]] == ["3003"]
    assert data["recent_inactive"][0]["pruned_at"] is not None


def test_grant_audit_unknown_grant_404(open_client, fake_ctx):
    role = _FakeRole(555)
    fake_ctx.bot = _FakeBot(_FakeGuild(role, []))
    resp = open_client.get("/api/reports/grant-audit?grant_name=nope")
    assert resp.status_code == 404


def test_grant_audit_no_guild_503(open_client):
    resp = open_client.get("/api/reports/grant-audit")
    assert resp.status_code == 503
