"""Tests for /api/home — the dashboard aggregation endpoint.

home.py is one big endpoint with field-group switching, so tests target:
- Empty/seeded shapes
- The ``fields=`` query filter
- Permission-based stripping of moderation/mod_actions groups
- Per-group SQL paths (messages, top_channels, top_users, mod_actions, etc.)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from bot_modules.core.db_utils import open_db


def _seed_messages(db_path, *, guild_id: int, rows):
    with open_db(db_path) as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO messages
                       (message_id, guild_id, channel_id, author_id, content, ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["message_id"], guild_id, r["channel_id"], r["author_id"], r.get("content", ""), r["ts"]),
            )


def _seed_audit(db_path, *, guild_id, action, actor_id=1, target_id=None, created_at=None):
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_log (guild_id, action, actor_id, target_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (guild_id, action, actor_id, target_id, created_at if created_at is not None else time.time()),
        )


# ── Shape & empty response ────────────────────────────────────────────


def test_home_returns_dict_for_admin_with_empty_db(authed_client):
    resp = authed_client.get("/api/home")
    assert resp.status_code == 200
    body = resp.json()
    # Privileged groups available to admin
    assert "msgs_1h" in body
    assert "msg_sparkline" in body
    assert len(body["msg_sparkline"]) == 24  # 24 hour buckets
    assert "recent_actions" in body  # admin → mod_actions group
    assert "latest_jail" in body  # admin → moderation group


# ── Field filter ──────────────────────────────────────────────────────


def test_home_fields_param_restricts_groups(authed_client):
    body = authed_client.get("/api/home?fields=messages").json()
    assert "msgs_1h" in body
    assert "msg_sparkline" in body
    assert "top_channels" not in body  # not requested
    assert "recent_actions" not in body


def test_home_fields_param_supports_multiple_groups(authed_client):
    body = authed_client.get("/api/home?fields=messages,top_users").json()
    assert "msgs_1h" in body
    assert "top_users" in body
    assert "top_channels" not in body


# ── Moderation group: economy claims ──────────────────────────────────


def test_home_moderation_counts_pending_claims(authed_client, fake_ctx):
    """The moderation group reports quest claims waiting on sign-off."""
    from bot_modules.services import economy_quests_service as quests_svc
    from bot_modules.services.economy_service import load_econ_settings

    body = authed_client.get("/api/home?fields=moderation").json()
    assert body["pending_claims"] == 0
    assert body["latest_claim"] is None

    quest = authed_client.post(
        "/api/economy/quests",
        json={
            "title": "Say hi",
            "description": "",
            "qtype": "daily",
            "reward": 15,
            "signoff": True,
            "criteria": "",
        },
    ).json()
    with open_db(fake_ctx.db_path) as conn:
        settings = load_econ_settings(conn, fake_ctx.guild_id)
        quests_svc.set_quest_active(conn, fake_ctx.guild_id, quest["id"], True)
        quests_svc.claim_quest(
            conn,
            settings,
            fake_ctx.guild_id,
            quest["id"],
            555,
            period="2026-07-10",
            booster=False,
        )

    body = authed_client.get("/api/home?fields=moderation").json()
    assert body["pending_claims"] == 1
    assert body["latest_claim"]["quest_title"] == "Say hi"
    assert body["latest_claim"]["user_id"] == "555"


# ── Permission-based group stripping ──────────────────────────────────


def test_home_strips_moderation_and_mod_actions_for_non_mod(fake_ctx):
    """A user with no perms must not receive moderation or mod_actions data."""
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
    from web_server.server import create_app

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    # permission_bits=0 → no admin, no moderator, no manage_server
    cookie = auth.create_session_cookie(
        user_id=99,
        username="rando",
        access_token="t",
        permission_bits=0,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)

    body = client.get("/api/home").json()
    assert "recent_actions" not in body  # admin-only group stripped
    assert "latest_jail" not in body  # moderator-only group stripped
    assert "msgs_1h" in body  # non-privileged group still present
    client.close()


# ── Message aggregations ──────────────────────────────────────────────


def test_home_message_counts_aggregate_recent_rows(authed_client, fake_ctx):
    now = int(time.time())
    _seed_messages(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        rows=[
            # Within 1h window
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": now - 60},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": now - 1200},
            # Within 24h window only
            {"message_id": 3, "channel_id": 10, "author_id": 200, "ts": now - 50000},
            # Stale (>30d)
            {"message_id": 4, "channel_id": 10, "author_id": 100, "ts": now - 86400 * 60},
        ],
    )
    body = authed_client.get("/api/home?fields=messages").json()
    assert body["msgs_1h"] == 2
    assert body["msgs_24h"] == 3
    # Unique authors today
    assert body["unique_today"] == 2


def test_home_top_channels_reports_top_by_count(authed_client, fake_ctx):
    """top_channels uses the last-hour window."""
    now = int(time.time())
    rows = (
        [{"message_id": i, "channel_id": 10, "author_id": 100, "ts": now - 60} for i in range(1, 6)]
        + [{"message_id": 100 + i, "channel_id": 20, "author_id": 100, "ts": now - 60} for i in range(1, 3)]
    )
    _seed_messages(fake_ctx.db_path, guild_id=fake_ctx.guild_id, rows=rows)

    body = authed_client.get("/api/home?fields=top_channels").json()
    top = body["top_channels"]
    assert top[0]["channel_id"] == "10"
    assert top[0]["count"] == 5
    assert top[1]["channel_id"] == "20"
    assert top[1]["count"] == 2


def test_home_top_users_reports_top_by_count(authed_client, fake_ctx):
    now = int(time.time())
    rows = (
        [{"message_id": i, "channel_id": 10, "author_id": 100, "ts": now - 60} for i in range(1, 4)]
        + [{"message_id": 100 + i, "channel_id": 10, "author_id": 200, "ts": now - 60} for i in range(1, 3)]
    )
    _seed_messages(fake_ctx.db_path, guild_id=fake_ctx.guild_id, rows=rows)

    body = authed_client.get("/api/home?fields=top_users").json()
    top = body["top_users"]
    assert top[0]["user_id"] == "100"
    assert top[0]["count"] == 3


def test_home_social_butterflies_excludes_bots(authed_client, fake_ctx):
    """A bot must not be ranked as a social butterfly, nor inflate a member's
    unique-partner count."""
    now = int(time.time())
    gid = fake_ctx.guild_id
    with open_db(fake_ctx.db_path) as conn:
        # Member 100 has 2 human partners; bot 999 "interacts with" many humans.
        for i, to in enumerate((200, 300)):
            conn.execute(
                "INSERT INTO user_interactions_log (guild_id, from_user_id, to_user_id, ts, message_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (gid, 100, to, now - 60, 10 + i),
            )
        for i, to in enumerate((200, 300, 400, 500)):
            conn.execute(
                "INSERT INTO user_interactions_log (guild_id, from_user_id, to_user_id, ts, message_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (gid, 999, to, now - 60, 20 + i),
            )
        # Member 100 also talks at the bot — must not count as a partner.
        conn.execute(
            "INSERT INTO user_interactions_log (guild_id, from_user_id, to_user_id, ts, message_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (gid, 100, 999, now - 60, 30),
        )
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, is_bot) VALUES (?, ?, 1)"
            " ON CONFLICT(guild_id, user_id) DO UPDATE SET is_bot = 1",
            (gid, 999),
        )

    butterflies = authed_client.get("/api/home?fields=butterflies").json()["social_butterflies"]
    ids = {b["user_id"] for b in butterflies}
    assert "999" not in ids  # bot is not a butterfly
    entry = next(b for b in butterflies if b["user_id"] == "100")
    assert entry["unique"] == 2  # the bot partner is excluded from the count


# ── Moderation/audit aggregations ─────────────────────────────────────


def test_home_recent_actions_returns_latest_audit_rows(authed_client, fake_ctx):
    """recent_actions is part of the mod_actions group (admin-only)."""
    _seed_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, action="jail", actor_id=1, target_id=42, created_at=100)
    _seed_audit(fake_ctx.db_path, guild_id=fake_ctx.guild_id, action="warn", actor_id=1, target_id=43, created_at=200)

    body = authed_client.get("/api/home?fields=mod_actions").json()
    actions = body["recent_actions"]
    assert len(actions) == 2
    assert actions[0]["action"] == "warn"  # newest first
    assert actions[0]["target_id"] == "43"


# ── Voice channel presence (live guild) ───────────────────────────────


def test_home_voice_channels_lists_populated_voice_rooms(authed_client, fake_ctx):
    """Voice channels with active non-bot members show up; empty ones are skipped."""
    populated_member = MagicMock()
    populated_member.id = 500
    populated_member.display_name = "Alice"
    populated_member.bot = False

    populated = MagicMock()
    populated.id = 9001
    populated.name = "Hangout"
    populated.members = [populated_member]

    empty = MagicMock()
    empty.id = 9002
    empty.name = "Empty Room"
    empty.members = []

    auth_user = MagicMock()
    auth_user.id = 1
    auth_user.bot = False
    auth_user.guild_permissions = MagicMock(value=0x8)
    auth_user.display_name = "tester"
    auth_user.status = "online"
    auth_user.joined_at = None
    role = MagicMock(id=0, name="@everyone")
    role.is_default = MagicMock(return_value=True)
    auth_user.roles = [role]

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.name = "Test Guild"
    guild.icon = None
    guild.member_count = 1
    guild.members = [auth_user]
    guild.voice_channels = [populated, empty]
    guild.channels = []
    guild.get_member = MagicMock(side_effect=lambda uid: auth_user if int(uid) == 1 else None)

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    body = authed_client.get("/api/home?fields=messages").json()
    # voice_channels is always at the top level regardless of fields filter
    assert "voice_channels" in body
    voice = body["voice_channels"]
    assert len(voice) == 1
    assert voice[0]["channel_name"] == "Hangout"
    assert voice[0]["members"][0]["user_name"] == "Alice"


# ── Auth gate ─────────────────────────────────────────────────────────


def test_home_requires_auth(fake_ctx):
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/home")
    assert resp.status_code in (401, 403)
    client.close()
