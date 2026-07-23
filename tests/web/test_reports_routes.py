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
    "/api/reports/one-sided-attention",
]


def test_one_sided_attention_flags_lopsided_pair(open_client, web_db):
    invalidate_report_cache()
    now = int(time.time())
    with open_db(web_db) as conn:
        # 20 one-directional replies/mentions, target never reciprocates.
        for i in range(20):
            conn.execute(
                """INSERT INTO user_interactions_log
                   (guild_id, from_user_id, to_user_id, ts, message_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (123, 4001, 4002, now - i * 3600, None),
            )
        conn.commit()
    resp = open_client.get("/api/reports/one-sided-attention?window_days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert "candidates" in data and data["window_days"] == 30
    pair = next(
        (c for c in data["candidates"] if c["from_id"] == "4001" and c["to_id"] == "4002"),
        None,
    )
    assert pair is not None
    assert pair["asymmetry"] == 1.0
    assert pair["ever_reciprocated"] is False
    assert any("never responded" in r for r in pair["reasons"])


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


# ── intake-report ─────────────────────────────────────────────────────


def test_intake_report_empty_shape(open_client):
    resp = open_client.get("/api/reports/intake-report")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["open_cards"] == []
    assert data["welcomers"] == []
    assert data["skipped_steps"] == []


def test_intake_report_with_data(open_client, fake_ctx):
    from bot_modules.services import intake_service as svc
    from web_server.deps import invalidate_report_cache

    gid = fake_ctx.guild_id
    now = time.time()
    with open_db(fake_ctx.db_path) as conn:
        open_card = svc.create_card(conn, gid, 7, now - 3600)
        assert open_card is not None
        svc.create_card(conn, gid, 8, now - 7200)
        svc.set_step_state(
            conn, open_card, "sfw_questions", done=True, actor_id=99, at=now - 1800
        )
        svc.complete_card(conn, gid, 8, 99, now - 3600)
    invalidate_report_cache()

    data = open_client.get("/api/reports/intake-report").json()
    # Snowflake-precision: ids as strings; open queue excludes the completed card.
    assert [c["user_id"] for c in data["open_cards"]] == ["7"]
    assert data["open_cards"][0]["done"] == 1
    assert data["counts"] == {"completed": 1}
    assert data["welcomers"][0]["user_id"] == "99"
    assert data["welcomers"][0]["completions"] == 1
    # Every default step except none was skipped on the completed card.
    skipped = {s["key"]: s["skipped"] for s in data["skipped_steps"]}
    assert skipped["sfw_questions"] == 1


def test_intake_report_panel_escapes_member_controlled_columns():
    """Source-scan guard (no Node in this repo): the intake-report panel must
    esc() the member-controlled name columns and the step label — they are
    interpolated into innerHTML by renderSortableTable (stored-XSS shape)."""
    from pathlib import Path

    src = Path("src/web_server/static/js/panels/intake-report.js").read_text(encoding="utf-8")
    assert 'format: (v, r) => esc(v || r.user_id)' in src  # both name columns
    assert src.count("esc(v || r.user_id)") == 2
    assert '{ key: "label", label: "Step", format: (v) => esc(v) }' in src
# ── time-to-level-5 name resolution ───────────────────────────────────


def test_time_to_level5_resolves_names_for_departed_members(open_client, fake_ctx):
    """A member the guild cache no longer knows must not render as a raw ID.

    resolve_names() only fills a name field that is falsy, so seeding the
    response with str(user_id) silently defeated both its known_users lookup
    and its "User <id>" fallback. This asserts the fallback actually fires.
    """
    invalidate_report_cache()
    guild_id = fake_ctx.guild_id
    user_id = 4242
    now = int(time.time())
    # Enough XP in one go to clear level 5, with a gap so the duration is > 0.
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            """INSERT INTO xp_events (guild_id, user_id, amount, source, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, user_id, 1, "message", now - 86400),
        )
        conn.execute(
            """INSERT INTO xp_events (guild_id, user_id, amount, source, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, user_id, 100_000, "message", now),
        )
        conn.commit()

    resp = open_client.get("/api/reports/time-to-level-5")
    assert resp.status_code == 200
    members = resp.json()["members"]
    assert members, "expected the seeded member to have reached level 5"
    row = next(m for m in members if int(m["user_id"]) == user_id)
    assert row["display_name"] != str(user_id)
    assert row["display_name"] == f"User {user_id}"
