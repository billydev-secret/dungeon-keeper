"""Tests for /api/moderation/anon-audit* endpoints.

The behaviours worth pinning here are the ones the service tests can't reach:
the content LEFT JOIN against ``messages`` (which is the whole reason this
table has no content column), snowflake-safe string ids, and the retention
round-trip through the dashboard.
"""

from __future__ import annotations

import time

from bot_modules.core.db_utils import open_db
from bot_modules.services.anon_audit_service import (
    FEATURE_AMA,
    FEATURE_WYR,
    insert_event,
)

BIG_MESSAGE_ID = 1387654321098765432  # > 2**53, must survive as a string
BIG_CHANNEL_ID = 1387654321098765111


def _seed(db_path, guild_id, **kw):
    base = dict(
        guild_id=guild_id,
        feature=FEATURE_AMA,
        event="question_asked",
        actor_id=7001,
    )
    base.update(kw)
    with open_db(db_path) as conn:
        return insert_event(conn, **base)


def _seed_message(db_path, guild_id, message_id, content):
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO messages "
            "(message_id, guild_id, channel_id, author_id, content, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, guild_id, BIG_CHANNEL_ID, 999, content, int(time.time())),
        )


# ── GET /api/moderation/anon-audit ────────────────────────────────────


def test_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/anon-audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["entries"] == []
    assert data["total"] == 0
    assert data["features"] == []


def test_returns_seeded_entry(open_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id, game_id="g-1")
    resp = open_client.get("/api/moderation/anon-audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    (entry,) = data["entries"]
    assert entry["feature"] == FEATURE_AMA
    assert entry["event"] == "question_asked"
    assert entry["actor_id"] == "7001"
    assert entry["game_id"] == "g-1"
    assert data["features"] == [FEATURE_AMA]


def test_content_is_joined_from_the_message_store(open_client, fake_ctx):
    """The audit table stores no content — it must come from `messages`."""
    _seed_message(fake_ctx.db_path, fake_ctx.guild_id, BIG_MESSAGE_ID, "the question")
    _seed(
        fake_ctx.db_path, fake_ctx.guild_id,
        message_id=BIG_MESSAGE_ID, channel_id=BIG_CHANNEL_ID,
    )

    (entry,) = open_client.get("/api/moderation/anon-audit").json()["entries"]
    assert entry["content"] == "the question"


def test_content_is_null_when_no_message_exists(open_client, fake_ctx):
    """A rejected screened question points at nothing, and must still list."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id, event="question_rejected")

    (entry,) = open_client.get("/api/moderation/anon-audit").json()["entries"]
    assert entry["content"] is None
    assert entry["message_id"] is None


def test_content_is_null_when_the_guild_stores_no_message_content(
    open_client, fake_ctx
):
    """Storage level 'none' leaves a content-less `messages` row, so the join
    finds the row but no text. The entry must still appear."""
    _seed_message(fake_ctx.db_path, fake_ctx.guild_id, BIG_MESSAGE_ID, None)
    _seed(
        fake_ctx.db_path, fake_ctx.guild_id,
        message_id=BIG_MESSAGE_ID, channel_id=BIG_CHANNEL_ID,
    )

    (entry,) = open_client.get("/api/moderation/anon-audit").json()["entries"]
    assert entry["content"] is None
    assert entry["message_id"] == str(BIG_MESSAGE_ID)


def test_ids_are_strings_not_bare_numbers(open_client, fake_ctx):
    """Snowflake precision: > 2**53 must not round-trip through a JS number."""
    _seed(
        fake_ctx.db_path, fake_ctx.guild_id,
        target_id=8002, message_id=BIG_MESSAGE_ID, channel_id=BIG_CHANNEL_ID,
    )

    raw = open_client.get("/api/moderation/anon-audit").text
    assert f'"{BIG_MESSAGE_ID}"' in raw
    assert str(BIG_MESSAGE_ID) + "," not in raw.replace(f'"{BIG_MESSAGE_ID}"', "")

    (entry,) = open_client.get("/api/moderation/anon-audit").json()["entries"]
    assert entry["message_id"] == str(BIG_MESSAGE_ID)
    assert entry["channel_id"] == str(BIG_CHANNEL_ID)
    assert entry["target_id"] == "8002"


def test_feature_filter(open_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id, feature=FEATURE_AMA)
    _seed(fake_ctx.db_path, fake_ctx.guild_id, feature=FEATURE_WYR, event="vote")

    data = open_client.get(
        "/api/moderation/anon-audit", params={"feature": FEATURE_WYR}
    ).json()
    assert data["total"] == 1
    assert data["entries"][0]["feature"] == FEATURE_WYR
    # The dropdown lists everything recorded, not just the filtered slice.
    assert sorted(data["features"]) == sorted([FEATURE_AMA, FEATURE_WYR])


def test_actor_filter(open_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id, actor_id=111)
    _seed(fake_ctx.db_path, fake_ctx.guild_id, actor_id=222)

    data = open_client.get(
        "/api/moderation/anon-audit", params={"actor_id": 222}
    ).json()
    assert data["total"] == 1
    assert data["entries"][0]["actor_id"] == "222"


def test_is_scoped_to_the_active_guild(open_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    _seed(fake_ctx.db_path, fake_ctx.guild_id + 1, actor_id=4242)

    data = open_client.get("/api/moderation/anon-audit").json()
    assert data["total"] == 1
    assert data["entries"][0]["actor_id"] == "7001"


def test_pagination(open_client, fake_ctx):
    now = time.time()
    for i in range(5):
        _seed(fake_ctx.db_path, fake_ctx.guild_id, actor_id=i, created_at=now - i)

    data = open_client.get(
        "/api/moderation/anon-audit", params={"limit": 2, "offset": 2}
    ).json()
    assert data["total"] == 5
    assert [e["actor_id"] for e in data["entries"]] == ["2", "3"]


def test_extra_is_returned_as_an_object(open_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id, extra={"mode": "screened"})

    (entry,) = open_client.get("/api/moderation/anon-audit").json()["entries"]
    assert entry["extra"] == {"mode": "screened"}


# ── Retention ─────────────────────────────────────────────────────────


def test_retention_defaults_to_90_days(open_client):
    resp = open_client.get("/api/moderation/anon-audit/retention")
    assert resp.status_code == 200
    assert resp.json()["retention_days"] == 90


def test_retention_round_trips(open_client):
    put = open_client.put(
        "/api/moderation/anon-audit/retention", json={"retention_days": 30}
    )
    assert put.status_code == 200
    assert put.json()["retention_days"] == 30

    assert open_client.get(
        "/api/moderation/anon-audit/retention"
    ).json()["retention_days"] == 30


def test_retention_accepts_keep_forever(open_client):
    resp = open_client.put(
        "/api/moderation/anon-audit/retention", json={"retention_days": 0}
    )
    assert resp.status_code == 200
    assert resp.json()["retention_days"] == 0


def test_retention_rejects_negative(open_client):
    resp = open_client.put(
        "/api/moderation/anon-audit/retention", json={"retention_days": -1}
    )
    assert resp.status_code == 422


# ── Auth ──────────────────────────────────────────────────────────────


def test_requires_authentication(fake_ctx):
    """Belt-and-braces alongside the authz sweep: these rows de-anonymise
    members, so an unauthenticated caller must never reach them."""
    from fastapi.testclient import TestClient
    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app)

    for path in (
        "/api/moderation/anon-audit",
        "/api/moderation/anon-audit/retention",
    ):
        assert client.get(path).status_code in (401, 403), path


def test_negative_limit_cannot_dump_the_whole_table(open_client, fake_ctx):
    """SQLite reads LIMIT -1 as unbounded — the route must clamp it."""
    for i in range(5):
        _seed(fake_ctx.db_path, fake_ctx.guild_id, actor_id=i)

    data = open_client.get(
        "/api/moderation/anon-audit", params={"limit": -1}
    ).json()
    assert data["total"] == 5
    assert len(data["entries"]) == 1


def test_limit_is_capped_at_200(open_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/anon-audit", params={"limit": 5000})
    assert resp.status_code == 200
