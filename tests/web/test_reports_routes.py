"""Tests for /api/reports/* endpoints.

Most tests are shape-only: seed minimal data, call the endpoint, assert the
response has the expected top-level keys and a non-error status.  Heavy
computation endpoints get a smoke-only test (200 + valid JSON).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.activity_graphs import overlay_period_start
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


@pytest.mark.parametrize(
    "resolution,points,x_label",
    [
        ("day_overlay", 24, "Hour of day"),
        ("week_overlay", 168, "Hour of week"),
    ],
)
def test_activity_overlay_shape(open_client, resolution, points, x_label):
    invalidate_report_cache()
    resp = open_client.get(
        f"/api/reports/activity?resolution={resolution}&mode=messages"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["labels"]) == points
    assert len(data["counts"]) == points
    assert data["x_label"] == x_label
    # The overlay drops both — the series axis is now "now vs history".
    assert data["series"] == []
    assert data["show_members"] is False


def _seed_overlay_history(db_path, guild_id, weeks):
    """One message and one XP event per week, `weeks` weeks back."""
    start = overlay_period_start(datetime.now(timezone.utc), 0.0, "week")
    with open_db(db_path) as conn:
        for back in range(1, weeks + 1):
            ts = start - back * 7 * 86400 + 1800
            conn.execute(
                "INSERT OR REPLACE INTO processed_messages (guild_id, message_id,"
                " channel_id, user_id, created_at, processed_at) VALUES (?,?,?,?,?,?)",
                (guild_id, 900000 + back, 55, 3001, ts, ts),
            )
            conn.execute(
                "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at)"
                " VALUES (?,?,?,?,?)",
                (guild_id, 3001, "text", 5.0, ts),
            )
        conn.commit()


@pytest.mark.parametrize(
    "mode,expected_sampled",
    [
        # 90 days of raw XP retention is 12 whole weeks; messages read the
        # whole archive, so the same request reaches all 20 weeks seeded.
        ("xp", 12),
        ("messages", 20),
    ],
)
def test_activity_overlay_clamps_compare_periods_by_mode(
    open_client, fake_ctx, mode, expected_sampled
):
    """The panel greys out 26 weeks in XP mode; the route is what enforces it."""
    invalidate_report_cache()
    _seed_overlay_history(fake_ctx.db_path, fake_ctx.guild_id, weeks=20)
    resp = open_client.get(
        f"/api/reports/activity?resolution=week_overlay&mode={mode}"
        "&compare_periods=26&include_bots=true"
    )
    assert resp.status_code == 200
    assert resp.json()["periods_sampled"] == expected_sampled


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
    "/api/reports/interaction-graph-series",
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
    resp = client.get("/api/reports/xp-leaderboard")
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


# ── inactive-report ───────────────────────────────────────────────────


def test_inactive_report_no_guild_503(open_client):
    resp = open_client.get("/api/reports/inactive-report")
    assert resp.status_code == 503


def test_inactive_report_role_scope_wiring(open_client, fake_ctx):
    """Route glue: live members + role scope reach the service, ids come back
    as strings, and the role filter actually excludes non-holders."""
    role_id = 555
    role = _FakeRole(role_id)
    holder = _FakeMember(3001, "Holder", roles=[role])
    outsider = _FakeMember(3002, "Outsider")
    role.members = [holder]
    guild = _FakeGuild(role, [holder, outsider])
    guild.members = [holder, outsider]
    fake_ctx.bot = _FakeBot(guild)

    resp = open_client.get(f"/api/reports/inactive-report?role_id={role_id}&days=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["role_name"] == "NSFW"
    assert [r["user_id"] for r in data["members"]] == ["3001"]
    assert data["total_scoped"] == 1

    resp = open_client.get(
        f"/api/reports/inactive-report?role_id={role_id}&role_mode=without&days=0"
    )
    assert [r["user_id"] for r in resp.json()["members"]] == ["3002"]


def test_inactive_report_unknown_role_404(open_client, fake_ctx):
    fake_ctx.bot = _FakeBot(_FakeGuild(_FakeRole(555), []))
    resp = open_client.get("/api/reports/inactive-report?role_id=999")
    assert resp.status_code == 404


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


def test_intake_report_panel_columns_are_not_rendered_as_html():
    """Source-scan guard (no Node in this repo) on the panel's XSS contract.

    The invariant is unchanged — the newcomer/welcomer names and the step label
    are member- and admin-supplied, and must never reach innerHTML as markup —
    but *who enforces it* moved. table.js escapes every cell by default now
    (S1, 2026-08-06 website review), so these columns must simply not opt out
    with `html: true`; the esc() wrappers they used to carry now double-escape,
    rendering a member called "Tom & Jerry" as "Tom &amp; Jerry".

    The general form of this check — every column of every table consumer, in
    both directions — lives in tests/web/test_frontend_wiring.py. This keeps a
    named guard on the panel whose columns are the most exposed.
    """
    from pathlib import Path

    src = Path("src/web_server/static/js/panels/intake-report.js").read_text(
        encoding="utf-8"
    )
    for key in ('key: "user_name"', 'key: "label"', 'key: "pending"'):
        assert key in src, f"{key} column disappeared — re-check this guard"
        at = src.index(key)
        spec = src[at : src.index("\n", at)]
        assert "html: true" not in spec, (
            f"{key} opted out of table.js escaping — it carries member-supplied "
            "text, so that is stored XSS in a moderator's dashboard"
        )
        assert "esc(" not in spec, (
            f"{key} escapes text that table.js already escapes (double-escaping)"
        )


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
