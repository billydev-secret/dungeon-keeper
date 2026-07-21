"""Tests for /api/messages/search and /api/messages/search/export.

These routes have a wide filter surface — author, channel, regex, sentiment,
emotion, attachments, reactions, time ranges, sort modes, paging. Each test
seeds a minimal corpus and exercises one filter or behavior.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.core.db_utils import open_db


# ── Test corpus ──────────────────────────────────────────────────────


def _seed(db_path, *, guild_id: int = 123, messages=None, attachments=None, reactions=None, mentions=None):
    """Insert a batch of messages and related rows.

    Each message dict: message_id, channel_id, author_id, content, reply_to_id,
    ts, sentiment (optional), emotion (optional).
    """
    messages = messages or []
    attachments = attachments or []
    reactions = reactions or []
    mentions = mentions or []

    with open_db(db_path) as conn:
        for m in messages:
            conn.execute(
                """INSERT INTO messages
                       (message_id, guild_id, channel_id, author_id, content,
                        reply_to_id, ts, sentiment, emotion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["message_id"],
                    guild_id,
                    m["channel_id"],
                    m["author_id"],
                    m.get("content"),
                    m.get("reply_to_id"),
                    m["ts"],
                    m.get("sentiment"),
                    m.get("emotion"),
                ),
            )
        for msg_id, url in attachments:
            conn.execute(
                "INSERT INTO message_attachments (message_id, url) VALUES (?, ?)",
                (msg_id, url),
            )
        for msg_id, emoji, count in reactions:
            conn.execute(
                "INSERT INTO message_reactions (message_id, emoji, count) VALUES (?, ?, ?)",
                (msg_id, emoji, count),
            )
        for msg_id, user_id in mentions:
            conn.execute(
                "INSERT INTO message_mentions (message_id, user_id) VALUES (?, ?)",
                (msg_id, user_id),
            )


# ── Search: empty / paging / shape ────────────────────────────────────


def test_search_empty_returns_zero(authed_client):
    body = authed_client.get("/api/messages/search").json()
    assert body == {"messages": [], "total": 0, "page": 1, "per_page": 50, "pages": 1}


def test_search_returns_seeded_messages_in_newest_order(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "first"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 200, "content": "second"},
            {"message_id": 3, "channel_id": 10, "author_id": 100, "ts": 300, "content": "third"},
        ],
    )
    body = authed_client.get("/api/messages/search").json()
    assert body["total"] == 3
    assert [m["content"] for m in body["messages"]] == ["third", "second", "first"]


def test_search_paginates(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": i, "channel_id": 10, "author_id": 100, "ts": i, "content": f"m{i}"}
            for i in range(1, 6)
        ],
    )
    body = authed_client.get("/api/messages/search?page=1&per_page=2").json()
    assert body["total"] == 5
    assert body["pages"] == 3
    assert len(body["messages"]) == 2


def test_search_page_two_returns_next_slice(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": i, "channel_id": 10, "author_id": 100, "ts": i, "content": f"m{i}"}
            for i in range(1, 6)
        ],
    )
    body = authed_client.get("/api/messages/search?page=2&per_page=2").json()
    assert body["page"] == 2
    contents = [m["content"] for m in body["messages"]]
    # newest first, page 1 = m5,m4 ; page 2 = m3,m2
    assert contents == ["m3", "m2"]


def test_search_isolates_by_guild(authed_client, fake_ctx):
    """Messages in another guild must not appear in the result."""
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[{"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "ours"}],
    )
    _seed(
        fake_ctx.db_path,
        guild_id=999,
        messages=[{"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "other"}],
    )
    body = authed_client.get("/api/messages/search").json()
    assert body["total"] == 1
    assert body["messages"][0]["content"] == "ours"


# ── Search filters ────────────────────────────────────────────────────


def test_search_filter_by_author_id(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "a"},
            {"message_id": 2, "channel_id": 10, "author_id": 200, "ts": 100, "content": "b"},
        ],
    )
    body = authed_client.get("/api/messages/search?author=100").json()
    assert {m["author_id"] for m in body["messages"]} == {"100"}


def test_search_filter_by_multiple_authors(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": i, "channel_id": 10, "author_id": 100 + (i % 3), "ts": i, "content": "x"}
            for i in range(1, 6)
        ],
    )
    body = authed_client.get("/api/messages/search?author=100&author=101").json()
    assert {m["author_id"] for m in body["messages"]} <= {"100", "101"}


def test_search_filter_by_channel(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "a"},
            {"message_id": 2, "channel_id": 20, "author_id": 100, "ts": 100, "content": "b"},
        ],
    )
    body = authed_client.get("/api/messages/search?channel=10").json()
    assert {m["channel_id"] for m in body["messages"]} == {"10"}


def test_search_filter_by_before_after(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "old"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 500, "content": "mid"},
            {"message_id": 3, "channel_id": 10, "author_id": 100, "ts": 900, "content": "new"},
        ],
    )
    body = authed_client.get("/api/messages/search?after=200&before=800").json()
    assert {m["content"] for m in body["messages"]} == {"mid"}


def test_search_filter_by_sentiment_range(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "x", "sentiment": -0.5},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "y", "sentiment": 0.0},
            {"message_id": 3, "channel_id": 10, "author_id": 100, "ts": 100, "content": "z", "sentiment": 0.5},
        ],
    )
    body = authed_client.get(
        "/api/messages/search?sentiment_min=-0.1&sentiment_max=0.6"
    ).json()
    assert {m["content"] for m in body["messages"]} == {"y", "z"}


def test_search_filter_by_emotion(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "happy", "emotion": "joy"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "mad", "emotion": "anger"},
        ],
    )
    body = authed_client.get("/api/messages/search?emotion=joy").json()
    assert {m["content"] for m in body["messages"]} == {"happy"}


def test_search_emotion_ignores_invalid_values(authed_client, fake_ctx):
    """Bogus emotion values are silently dropped — not 400."""
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "x", "emotion": "joy"},
        ],
    )
    body = authed_client.get("/api/messages/search?emotion=NOT_A_REAL_ONE,joy").json()
    assert body["total"] == 1


def test_search_filter_by_min_max_length(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "short"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "x" * 50},
        ],
    )
    body = authed_client.get("/api/messages/search?min_length=10&max_length=100").json()
    assert {m["message_id"] for m in body["messages"]} == {"2"}


def test_search_filter_has_attachments(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "with"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "without"},
        ],
        attachments=[(1, "https://cdn.example/img.png")],
    )
    body = authed_client.get("/api/messages/search?has_attachments=true").json()
    assert {m["message_id"] for m in body["messages"]} == {"1"}
    assert body["messages"][0]["attachments"] == ["https://cdn.example/img.png"]

    body = authed_client.get("/api/messages/search?has_attachments=false").json()
    assert {m["message_id"] for m in body["messages"]} == {"2"}


def test_search_filter_has_reactions(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "with"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "without"},
        ],
        reactions=[(1, "🔥", 3)],
    )
    body = authed_client.get("/api/messages/search?has_reactions=true").json()
    assert {m["message_id"] for m in body["messages"]} == {"1"}


def test_search_filter_by_mentions_user_id(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "mentioning"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "no-mention"},
        ],
        mentions=[(1, 500)],
    )
    body = authed_client.get("/api/messages/search?mentions=500").json()
    assert {m["message_id"] for m in body["messages"]} == {"1"}


def test_search_returns_400_for_bad_regex(authed_client):
    resp = authed_client.get("/api/messages/search?regex=[unterminated")
    assert resp.status_code == 400
    assert "Invalid regex" in resp.json()["detail"]


def test_search_regex_filters_results(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "hello world"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "goodbye moon"},
        ],
    )
    body = authed_client.get("/api/messages/search?regex=hello").json()
    assert {m["content"] for m in body["messages"]} == {"hello world"}


# ── Search: sort modes ────────────────────────────────────────────────


def test_search_sort_oldest_first(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "a"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 200, "content": "b"},
        ],
    )
    body = authed_client.get("/api/messages/search?sort=oldest").json()
    assert [m["content"] for m in body["messages"]] == ["a", "b"]


def test_search_sort_most_reacted(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "popular"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 200, "content": "quiet"},
        ],
        reactions=[(1, "🔥", 10)],
    )
    body = authed_client.get("/api/messages/search?sort=most_reacted").json()
    assert body["messages"][0]["content"] == "popular"


def test_search_sort_longest_first(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "tiny"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "x" * 100},
        ],
    )
    body = authed_client.get("/api/messages/search?sort=longest").json()
    assert body["messages"][0]["message_id"] == "2"


def test_search_sort_most_positive_and_negative(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "down", "sentiment": -0.8},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 100, "content": "up", "sentiment": 0.8},
        ],
    )
    pos = authed_client.get("/api/messages/search?sort=most_positive").json()
    neg = authed_client.get("/api/messages/search?sort=most_negative").json()
    assert pos["messages"][0]["content"] == "up"
    assert neg["messages"][0]["content"] == "down"


# ── Reply target resolution ───────────────────────────────────────────


def test_search_resolves_reply_target_author(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 500, "ts": 100, "content": "original"},
            {"message_id": 2, "channel_id": 10, "author_id": 100, "ts": 200, "content": "reply", "reply_to_id": 1},
        ],
    )
    body = authed_client.get("/api/messages/search").json()
    by_id = {m["message_id"]: m for m in body["messages"]}
    reply = by_id["2"]
    assert reply["reply_to_id"] == "1"
    assert reply["reply_to_author_id"] == "500"


# ── Export ────────────────────────────────────────────────────────────


def test_export_returns_json_attachment(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "exportable"},
        ],
    )
    resp = authed_client.get("/api/messages/search/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers["content-disposition"]
    body = json.loads(resp.text)
    assert body["total"] == 1
    assert body["messages"][0]["content"] == "exportable"


def test_export_returns_400_for_bad_regex(authed_client):
    resp = authed_client.get("/api/messages/search/export?regex=(*invalid")
    assert resp.status_code == 400


def test_export_filter_by_author_id(authed_client, fake_ctx):
    _seed(
        fake_ctx.db_path,
        guild_id=fake_ctx.guild_id,
        messages=[
            {"message_id": 1, "channel_id": 10, "author_id": 100, "ts": 100, "content": "mine"},
            {"message_id": 2, "channel_id": 10, "author_id": 200, "ts": 100, "content": "theirs"},
        ],
    )
    resp = authed_client.get("/api/messages/search/export?author=100")
    body = json.loads(resp.text)
    assert body["total"] == 1
    assert body["messages"][0]["author_id"] == "100"


# ── Auth gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["/api/messages/search", "/api/messages/search/export"],
)
def test_messages_routes_require_auth(fake_ctx, path):
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(path)
    assert resp.status_code in (401, 403)
    client.close()
