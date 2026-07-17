"""Tests for /api/chat-revive/* — the feature's whole management surface."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot_modules.chat_revive.starter_pack import STARTER_QUESTIONS

GID = 123  # FakeCtx default guild


class _Channel:
    """Just enough discord.TextChannel for the routes; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self, cid: int) -> None:
        self.id = cid
        self.name = "general"
        self.slowmode_delay = 0
        self.send = AsyncMock(return_value=SimpleNamespace(id=777))

    def is_nsfw(self) -> bool:
        return False


def _wire_channel(fake_ctx, cid: int = 555) -> _Channel:
    channel = _Channel(cid)
    guild = SimpleNamespace(
        id=fake_ctx.guild_id,
        get_channel=lambda c: channel if c == cid else None,
    )
    fake_ctx.bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == fake_ctx.guild_id else None
    )
    return channel


def _enable(client, **overrides):
    body = {
        "enabled": True,
        "role_id": 999,
        "quiet_start": 0,
        "quiet_end": 8,
        "daily_budget": 3,
        "guild_gap_minutes": 90,
        "flourish_enabled": True,
    }
    body.update(overrides)
    r = client.put("/api/chat-revive/config", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── config ────────────────────────────────────────────────────────────


def test_overview_defaults(open_client):
    r = open_client.get("/api/chat-revive/overview")
    assert r.status_code == 200
    data = r.json()
    assert data["config"]["enabled"] is False
    assert data["config"]["daily_budget"] == 3
    assert data["config"]["ping_max_per_day"] == 3
    assert data["config"]["ping_cooldown_minutes"] == 60
    assert data["channels"] == []
    assert data["bank_size"] == 0
    assert "general" in data["categories"]


def test_enable_seeds_starter_pack_once(open_client):
    first = _enable(open_client)
    assert first["seeded"] == len(STARTER_QUESTIONS)
    second = _enable(
        open_client, daily_budget=2, ping_max_per_day=5, ping_cooldown_minutes=30
    )
    assert second["seeded"] == 0
    assert second["config"]["daily_budget"] == 2
    assert second["config"]["ping_max_per_day"] == 5
    assert second["config"]["ping_cooldown_minutes"] == 30
    overview = open_client.get("/api/chat-revive/overview").json()
    assert overview["bank_size"] == len(STARTER_QUESTIONS)


def test_channel_roundtrip_and_removal(open_client):
    r = open_client.put(
        "/api/chat-revive/channels/555",
        json={"categories": ["Deep", "music", "deep"], "ping_enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["channel"]["categories"] == ["deep", "music"]
    overview = open_client.get("/api/chat-revive/overview").json()
    assert [c["channel_id"] for c in overview["channels"]] == ["555"]

    assert open_client.delete("/api/chat-revive/channels/555").status_code == 200
    assert open_client.delete("/api/chat-revive/channels/555").status_code == 404


def test_channel_id_survives_as_full_precision_string(open_client):
    # Real Discord snowflakes exceed JS's Number.MAX_SAFE_INTEGER (2**53); the
    # API must emit them as strings so a JSON `Number` round-trip can't round
    # them to a different, nonexistent channel (regression: #chat-revive-404).
    big_cid = 1469123456789012345
    r = open_client.put(f"/api/chat-revive/channels/{big_cid}", json={})
    assert r.status_code == 200
    assert r.json()["channel"]["channel_id"] == str(big_cid)
    overview = open_client.get("/api/chat-revive/overview").json()
    assert [c["channel_id"] for c in overview["channels"]] == [str(big_cid)]


def test_channel_rejects_bad_categories(open_client):
    r = open_client.put(
        "/api/chat-revive/channels/555", json={"categories": ["no good!"]}
    )
    assert r.status_code == 422


# ── question bank ─────────────────────────────────────────────────────


def test_question_add_duplicate_retire(open_client):
    r = open_client.post(
        "/api/chat-revive/questions",
        json={"text": "Fresh one?", "category": "deep"},
    )
    assert r.status_code == 200
    qid = r.json()["id"]
    dup = open_client.post("/api/chat-revive/questions", json={"text": "fresh one?"})
    assert dup.status_code == 409

    listed = open_client.get("/api/chat-revive/questions").json()["questions"]
    assert [q["text"] for q in listed] == ["Fresh one?"]

    assert (
        open_client.post(f"/api/chat-revive/questions/{qid}/retire").status_code == 200
    )
    assert open_client.post("/api/chat-revive/questions/9999/retire").status_code == 404
    assert open_client.get("/api/chat-revive/questions").json()["questions"] == []
    retired = open_client.get(
        "/api/chat-revive/questions", params={"include_retired": True}
    ).json()["questions"]
    assert len(retired) == 1 and retired[0]["active"] is False


def test_question_bulk(open_client):
    r = open_client.post(
        "/api/chat-revive/questions/bulk",
        json={"lines": "One?\nspicy,nsfw: Hot?\nOne?\n"},
    )
    assert r.status_code == 200
    assert r.json() == {"added": 2, "skipped": 1}
    qs = open_client.get("/api/chat-revive/questions").json()["questions"]
    spicy = next(q for q in qs if q["text"] == "Hot?")
    assert spicy["nsfw"] is True and spicy["category"] == "spicy"


# ── stats & check ─────────────────────────────────────────────────────


def test_stats_empty_then_seeded(open_client, fake_ctx):
    assert open_client.get("/api/chat-revive/stats").json()["total"] == 0
    now = time.time()
    with fake_ctx.open_db() as conn:
        conn.execute(
            "INSERT INTO revive_events (guild_id, channel_id, trigger_kind, pinged,"
            " local_day, created_at, measured_at, success, follow_msgs,"
            " follow_authors) VALUES (?, 555, 'auto', 0, '2026-07-14', ?, ?, 1, 4, 2)",
            (GID, now - 600, now),
        )
    s = open_client.get("/api/chat-revive/stats").json()
    assert s["total"] == 1 and s["successes"] == 1
    assert s["channels"][0]["channel_id"] == "555"


def test_check_explains_without_bot(open_client):
    # quiet_start == quiet_end disables the quiet-hours gate — otherwise this
    # test is flaky, since /check uses the real wall clock and would report
    # "Quiet hours" instead of "No message history" whenever it happens to
    # run during the default 00:00-08:00 local window.
    _enable(open_client, quiet_start=0, quiet_end=0)
    open_client.put("/api/chat-revive/channels/555", json={})
    r = open_client.get("/api/chat-revive/check/555")
    assert r.status_code == 200
    data = r.json()
    assert data["would_fire"] is False
    assert "No message history" in data["reason"]
    assert data["live_channel"] is False
    assert data["would_ask"] is not None  # starter pack was seeded


# ── discord-side actions ──────────────────────────────────────────────


def test_fire_without_bot_is_503(open_client):
    _enable(open_client)
    r = open_client.post("/api/chat-revive/fire", json={"channel_id": 555})
    assert r.status_code == 503


def test_fire_posts_and_records(open_client, fake_ctx):
    _enable(open_client)
    channel = _wire_channel(fake_ctx)
    r = open_client.post("/api/chat-revive/fire", json={"channel_id": 555})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["pinged"] is False
    channel.send.assert_awaited_once()
    assert body["question"] in channel.send.await_args.args[0]
    with fake_ctx.open_db() as conn:
        row = conn.execute("SELECT * FROM revive_events").fetchone()
    assert row["trigger_kind"] == "manual" and row["message_id"] == 777


def test_fire_conflict_when_disabled_or_empty(open_client, fake_ctx):
    _wire_channel(fake_ctx)
    r = open_client.post("/api/chat-revive/fire", json={"channel_id": 555})
    assert r.status_code == 409  # guild not enabled


def test_optin_post_needs_role_then_posts(open_client, fake_ctx):
    channel = _wire_channel(fake_ctx)
    _enable(open_client, role_id=None)
    assert (
        open_client.post("/api/chat-revive/optin-post", json={"channel_id": 555})
        .status_code
        == 409
    )
    _enable(open_client, role_id=999)
    r = open_client.post("/api/chat-revive/optin-post", json={"channel_id": 555})
    assert r.status_code == 200
    view = channel.send.await_args.kwargs["view"]
    assert [item.custom_id for item in view.children] == ["chat_revive_optin:999"]
