"""Tests for /api/reports/* endpoints.

Most tests are shape-only: seed minimal data, call the endpoint, assert the
response has the expected top-level keys and a non-error status.  Heavy
computation endpoints get a smoke-only test (200 + valid JSON).
"""

from __future__ import annotations

import time

import pytest

from db_utils import open_db
from web.deps import invalidate_report_cache


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
    from web.auth import DiscordOAuthAuth
    from web.server import create_app
    from fastapi.testclient import TestClient
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/reports/role-growth")
    assert resp.status_code in (401, 403)
    client.close()
