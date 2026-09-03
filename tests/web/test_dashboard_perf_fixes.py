"""Regression tests for the 2026-08-06 website review's performance findings.

The dashboard runs uvicorn on the **bot's own event loop, in the bot's
process**, so "does this run on the loop" is a correctness property, not a
micro-optimisation: a second of PIL CPU inline is a second of missed Discord
heartbeats. The loop-safety tests here therefore assert the *observable*
property — the blocking work ran somewhere with no running event loop — rather
than timing anything.

Covers:
  B-PERF3  PIL image work moved off the loop (quote border, economy icon)
  B-PERF4  sentiment reads hit ``messages`` and go through the tile cache
  B-PERF5  the advisor's context build runs off the loop
  B-PERF6  health deep-dives use the cache, with collision-free keys
  B-PERF7  two admin-surface N+1 loops collapsed into one query
  B-SEC9   the avatar fetch connects to the address it validated
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from bot_modules.core.db_utils import open_db
from tests.web.conftest import StubMember


# ── helpers ──────────────────────────────────────────────────────────────


def _on_loop() -> bool:
    """True when called from a thread that is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@contextmanager
def sql_trace(fake_ctx, sink: list[str]):
    """Record every SQL statement the app runs through ``ctx.open_db()``."""
    original = fake_ctx.open_db

    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(sink.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    fake_ctx.open_db = traced
    try:
        yield sink
    finally:
        fake_ctx.open_db = original


def _frame_png() -> bytes:
    img = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([0, 0, 59, 39], outline=(200, 160, 40, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _icon_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (10, 120, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _cache_keys(fake_ctx) -> set[str]:
    with fake_ctx.open_db() as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT metric_key FROM health_metrics_cache WHERE guild_id = ?",
                (fake_ctx.guild_id,),
            )
        }


# ── B-PERF3 — PIL work must not run on the event loop ────────────────────


def test_quote_border_upload_decodes_off_the_event_loop(authed_client, monkeypatch):
    """The 0.5–2 s decode/alpha-scan/thumbnail/analyze block runs in a thread.

    An 8 MB upload used to hold the loop for the whole of it, stalling the
    Discord gateway and every other dashboard request.
    """
    from web_server.routes import config as config_routes

    seen: list[bool] = []
    real = config_routes._install_quote_border

    def _spy(content, target):
        seen.append(_on_loop())
        return real(content, target)

    monkeypatch.setattr(config_routes, "_install_quote_border", _spy)

    resp = authed_client.post(
        "/api/config/quote-border",
        files={"file": ("frame.png", _frame_png(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [False], "image processing ran on the event loop"


def test_economy_icon_upload_normalises_off_the_event_loop(authed_client, monkeypatch):
    from web_server.routes import economy as economy_routes

    seen: list[bool] = []
    real = economy_routes._normalize_icon

    def _spy(content):
        seen.append(_on_loop())
        return real(content)

    monkeypatch.setattr(economy_routes, "_normalize_icon", _spy)

    resp = authed_client.post(
        "/api/economy/icon-catalog",
        data={"name": "sparkle", "price": "50"},
        files={"image": ("icon.png", _icon_png(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [False], "icon normalisation ran on the event loop"


def test_quote_border_guards_survive_the_move_off_the_loop(authed_client):
    """The rejections moved into the worker thread must still reach the client.

    ``HTTPException`` raised inside ``asyncio.to_thread`` has to propagate
    unchanged, or every validation failure would surface as a 500.
    """
    opaque = io.BytesIO()
    Image.new("RGBA", (60, 40), (10, 20, 30, 255)).save(opaque, format="PNG")
    resp = authed_client.post(
        "/api/config/quote-border",
        files={"file": ("solid.png", opaque.getvalue(), "image/png")},
    )
    assert resp.status_code == 400
    assert "transparent" in resp.json()["detail"].lower()


# ── B-PERF4 — sentiment reads ────────────────────────────────────────────
#
# ``messages`` carries duplicated sentiment/emotion columns indexed as
# (guild_id, sentiment); ``message_sentiment`` only has guild-prefix indexes, so
# the old join scanned + did a rowid lookup per row + sorted in a temp b-tree.
# Seeding *only* ``messages`` proves the switch actually happened — under the
# old SQL the join would find nothing and the feed would come back empty.


def _seed_sentiment_messages(fake_ctx, *, count: int = 3) -> None:
    with fake_ctx.open_db() as conn:
        for i in range(count):
            conn.execute(
                "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
                " content, ts, sentiment, emotion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    9000 + i,
                    fake_ctx.guild_id,
                    77,
                    500 + i,
                    f"delightful message {i}",
                    1785000000 + i,
                    0.9,
                    "joy",
                ),
            )


def test_sentiment_feed_reads_the_messages_table(open_client, fake_ctx):
    _seed_sentiment_messages(fake_ctx)
    body = open_client.get("/api/health/tiles?tiles=sentiment_feed").json()
    feed = body["tiles"]["sentiment_feed"]
    assert [m["message_id"] for m in feed["messages"]] == ["9002", "9001", "9000"]
    # No message_sentiment rows exist at all — the join form returned nothing.
    with fake_ctx.open_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_sentiment").fetchone()[0] == 0


def test_sentiment_feed_tile_is_served_from_the_cache(open_client, fake_ctx):
    """The one tile that used to skip the 15-minute cache its siblings use."""
    _seed_sentiment_messages(fake_ctx)
    first = open_client.get("/api/health/tiles?tiles=sentiment_feed").json()
    assert first["tiles"]["sentiment_feed"]["messages"]

    # Delete the source rows. A cached tile is unaffected; an uncached one
    # would now come back empty.
    with fake_ctx.open_db() as conn:
        conn.execute("DELETE FROM messages")

    second = open_client.get("/api/health/tiles?tiles=sentiment_feed").json()
    assert second["tiles"]["sentiment_feed"] == first["tiles"]["sentiment_feed"]


def test_sentiment_feed_cache_does_not_bleed_across_include_bots(
    open_client, fake_ctx
):
    _seed_sentiment_messages(fake_ctx)
    open_client.get("/api/health/tiles?tiles=sentiment_feed")
    open_client.get("/api/health/tiles?tiles=sentiment_feed&include_bots=true")
    keys = _cache_keys(fake_ctx)
    assert "sentiment_feed" in keys
    assert "sentiment_feed+bots" in keys


@pytest.mark.parametrize("tile", ["sentiment_feed", "sentiment"])
def test_sentiment_tiles_never_touch_message_sentiment(open_client, fake_ctx, tile):
    """No sentiment read anywhere in the tiles path names the scanned table.

    ``compute_sentiment`` in ``services/health_metrics.py`` was the last reader
    holding ``message_sentiment`` on the hot path; its own equivalence tests
    live in ``tests/test_health_metrics.py``.
    """
    _seed_sentiment_messages(fake_ctx)
    sink: list[str] = []
    with sql_trace(fake_ctx, sink):
        open_client.get(f"/api/health/tiles?tiles={tile}")
    assert not [s for s in sink if "message_sentiment" in s]


def test_sentiment_outliers_are_read_from_the_messages_table(
    open_client, authed_client, fake_ctx
):
    """The 1-sigma outlier block moved off the join too.

    With rows only in ``messages``, the old ``message_sentiment`` join would
    return nothing and both outlier lists would come back empty.
    """
    # A flat neutral bulk keeps the 1-sigma threshold well below the two
    # extremes, so the assertion is about the query, not about the statistics.
    rows = [(9200 + i, 0.0, "neutral") for i in range(10)]
    rows.append((9300, 0.95, "joy"))
    rows.append((9301, -0.95, "anger"))
    with fake_ctx.open_db() as conn:
        for mid, score, emotion in rows:
            conn.execute(
                "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
                " content, ts, sentiment, emotion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, fake_ctx.guild_id, 77, 600, "text", 1785000000, score, emotion),
            )

    body = authed_client.get("/api/health/tiles?tiles=sentiment").json()
    outliers = body["tiles"]["sentiment"]["outliers"]
    assert [m["message_id"] for m in outliers["top"]] == ["9300"]
    assert [m["message_id"] for m in outliers["bottom"]] == ["9301"]


# ── B-PERF6 — deep-dive cache, and the keys that make it safe ────────────


def test_deep_key_separates_every_result_changing_parameter():
    """A key collision here serves one population's numbers under another's
    filters — strictly worse than the recompute it replaces."""
    from bot_modules.services.health_service import cache_key
    from web_server.routes.health import _deep_key

    plain = _deep_key("gini")
    with_bots = _deep_key("gini", include_bots=True)
    seven = _deep_key("mod_engagement", days=7)
    thirty = _deep_key("mod_engagement", days=30)

    assert len({plain, with_bots, seven, thirty}) == 4
    # And a deep-dive payload can never be served where a tile payload is
    # expected: the two have different shapes for the same metric.
    assert plain != cache_key("gini")
    assert with_bots != cache_key("gini", include_bots=True)


def test_deep_key_is_stable_regardless_of_kwarg_order():
    from web_server.routes.health import _deep_key

    assert _deep_key("x", days=7, window=3) == _deep_key("x", window=3, days=7)


@pytest.mark.parametrize(
    "path",
    [
        "/api/health/dau-mau",
        "/api/health/heatmap",
        "/api/health/gini",
        "/api/health/social-graph",
        "/api/health/sentiment",
        "/api/health/newcomer-funnel",
        "/api/health/cohort-retention",
        "/api/health/mod-engagement",
        "/api/health/channel-health",
        "/api/health/mod-workload",
        "/api/health/sentiment-feed",
    ],
)
def test_deep_dive_writes_a_namespaced_cache_entry(
    open_client, fake_ctx, live_guild, path
):
    # A live guild is required: the mod/newcomer metrics deliberately skip the
    # cache when the guild isn't available (see test_health_degraded_cache).
    live_guild([StubMember(42, joined_at=datetime.now(timezone.utc))])
    assert open_client.get(path).status_code == 200
    assert any(k.startswith("deep:") for k in _cache_keys(fake_ctx)), path


def test_deep_dive_second_call_is_served_from_the_cache(open_client, fake_ctx):
    _seed_sentiment_messages(fake_ctx)
    first = open_client.get("/api/health/sentiment-feed").json()
    assert first["messages"]

    with fake_ctx.open_db() as conn:
        conn.execute("DELETE FROM messages")

    second = open_client.get("/api/health/sentiment-feed").json()
    assert [m["message_id"] for m in second["messages"]] == [
        m["message_id"] for m in first["messages"]
    ]


def test_include_bots_deep_dives_get_separate_cache_entries(open_client, fake_ctx):
    open_client.get("/api/health/gini")
    open_client.get("/api/health/gini?include_bots=true")
    deep = {k for k in _cache_keys(fake_ctx) if k.startswith("deep:gini")}
    assert deep == {"deep:gini", "deep:gini+bots"}


def test_mod_engagement_days_get_separate_cache_entries(
    open_client, fake_ctx, live_guild
):
    live_guild([StubMember(42, joined_at=datetime.now(timezone.utc))])
    open_client.get("/api/health/mod-engagement?days=7")
    open_client.get("/api/health/mod-engagement?days=30")
    deep = {k for k in _cache_keys(fake_ctx) if k.startswith("deep:mod_engagement")}
    assert deep == {"deep:mod_engagement|days=7", "deep:mod_engagement|days=30"}


def test_deep_dive_display_names_are_not_frozen_into_the_cache(
    open_client, fake_ctx
):
    """Names resolve after the cache read, so a rename shows up next request."""
    _seed_sentiment_messages(fake_ctx, count=1)
    with fake_ctx.open_db() as conn:
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, username, display_name,"
            " updated_at, is_bot) VALUES (?, ?, ?, ?, ?, 0)",
            (fake_ctx.guild_id, 500, "old", "Old Name", 1785000000),
        )
    assert open_client.get("/api/health/sentiment-feed").json()["messages"][0][
        "author_name"
    ] == "Old Name"

    with fake_ctx.open_db() as conn:
        conn.execute(
            "UPDATE known_users SET display_name = 'New Name' WHERE user_id = 500"
        )

    body = open_client.get("/api/health/sentiment-feed").json()
    assert body["messages"][0]["author_name"] == "New Name"


# ── B-PERF7 — N+1 loops over small admin tables ──────────────────────────


def test_legitlibs_template_list_issues_one_query(open_client, fake_ctx):
    with fake_ctx.open_db() as conn:
        for i in range(4):
            conn.execute(
                "INSERT INTO legitlibs_templates (template_id, title, body, tier,"
                " tags, status, blanks, guild_id) VALUES (?, ?, '', 1, '', 'draft',"
                " ?, ?)",
                (i + 1, f"t{i}", json.dumps(["a", "b", "c"]), fake_ctx.guild_id),
            )

    sink: list[str] = []
    with sql_trace(fake_ctx, sink):
        body = open_client.get("/api/games/legitlibs/templates").json()

    assert [t["blanks_count"] for t in body["templates"]] == [3, 3, 3, 3]
    hits = [s for s in sink if "legitlibs_templates" in s]
    assert len(hits) == 1, hits


def test_community_quest_progress_is_read_in_one_query(open_client, fake_ctx):
    from bot_modules.services import economy_quests_service as quests_svc

    with fake_ctx.open_db() as conn:
        for i in range(3):
            qid = quests_svc.create_quest(
                conn,
                fake_ctx.guild_id,
                title=f"community {i}",
                description="",
                qtype="community",
                reward=10,
                signoff=0,
                criteria="",
                starts_at=None,
                ends_at=None,
                rotate_tag="",
                community_target=100,
                created_by=None,
            )
            conn.execute(
                "INSERT INTO econ_community_progress (quest_id, current) VALUES (?, ?)",
                (qid, 10 * (i + 1)),
            )

    sink: list[str] = []
    with sql_trace(fake_ctx, sink):
        body = open_client.get("/api/economy/quests").json()

    assert sorted(q["community_current"] for q in body["quests"]) == [10, 20, 30]
    hits = [s for s in sink if "econ_community_progress" in s]
    assert len(hits) == 1, hits


def test_community_quest_without_progress_row_reports_zero(open_client, fake_ctx):
    """The bulk read must not invent a row where the per-quest SELECT found none."""
    from bot_modules.services import economy_quests_service as quests_svc

    with fake_ctx.open_db() as conn:
        quests_svc.create_quest(
            conn,
            fake_ctx.guild_id,
            title="untouched",
            description="",
            qtype="community",
            reward=10,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=100,
            created_by=None,
        )

    quest = open_client.get("/api/economy/quests").json()["quests"][0]
    assert quest["community_current"] == 0
    assert quest["community_completed_at"] is None
    assert quest["community_settled_at"] is None


# ── B-PERF5 — the advisor's context build runs off the loop ──────────────


def test_advisor_builds_its_context_off_the_event_loop(
    open_client, fake_ctx, monkeypatch
):
    """Four config reads + ``build_asker_context`` (which reopens the DB) +
    a per-channel permission walk used to run inline in the async handler."""
    from unittest.mock import MagicMock

    from bot_modules.services import advisor_context, advisor_service

    with fake_ctx.open_db() as conn:
        advisor_service.set_advisor_context_enabled(conn, True, fake_ctx.guild_id)

    guild = MagicMock()
    guild.get_member.return_value = None
    bot = MagicMock()
    bot.get_guild.return_value = guild
    fake_ctx.bot = bot

    seen: list[bool] = []

    def _context(*_a, **_kw):
        seen.append(_on_loop())
        return "server context"

    monkeypatch.setattr(advisor_context, "build_asker_context", _context)
    monkeypatch.setattr(advisor_context, "is_staff", lambda _m: False)
    monkeypatch.setattr(advisor_context, "visible_text_channels", lambda *_a: [])

    async def _answer(*_a, **_kw):
        return advisor_service.AdvisorResult(True, "hi")

    monkeypatch.setattr(advisor_service, "answer_advisor", _answer)

    try:
        resp = open_client.post("/api/help/advisor", json={"question": "hello?"})
    finally:
        fake_ctx.bot = None

    assert resp.status_code == 200, resp.text
    assert seen == [False], "advisor context was built on the event loop"


def test_advisor_config_tools_run_off_the_event_loop(open_client, fake_ctx, monkeypatch):
    """The residue of B-PERF5: the route's ``fetch_settings``/``fetch_gaps``
    lambdas are handed to the service and called back from inside the tool
    loop, so wrapping them at the route was impossible — ``_run_tool`` is what
    had to become awaitable. Proves the whole wiring, not just the service."""
    from unittest.mock import AsyncMock, MagicMock

    from anthropic.types import TextBlock, ToolUseBlock

    from bot_modules.services import advisor_context, advisor_gaps, advisor_service

    with fake_ctx.open_db() as conn:
        advisor_service.set_advisor_context_enabled(conn, True, fake_ctx.guild_id)

    guild = MagicMock()
    guild.get_member.return_value = MagicMock()  # a resolvable member
    bot = MagicMock()
    bot.get_guild.return_value = guild
    fake_ctx.bot = bot

    seen: dict[str, bool] = {}

    monkeypatch.setattr(advisor_context, "can_see_config", lambda _m: True)
    monkeypatch.setattr(advisor_context, "is_staff", lambda _m: False)
    monkeypatch.setattr(advisor_context, "visible_text_channels", lambda *_a: [])
    monkeypatch.setattr(
        advisor_context, "build_asker_context", lambda *_a, **_kw: "server context"
    )
    def _probe(label: str, payload: str) -> str:
        seen[label] = _on_loop()
        return payload

    monkeypatch.setattr(
        advisor_context,
        "fetch_feature_settings",
        lambda *_a: _probe("fetch_settings", "daily_reward = 100"),
    )
    monkeypatch.setattr(
        advisor_gaps, "fetch_setup_gaps", lambda *_a: _probe("fetch_gaps", "- no gaps")
    )

    def _resp(*blocks, stop_reason="end_turn"):
        r = MagicMock()
        r.content = list(blocks)
        r.stop_reason = stop_reason
        return r

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _resp(
            ToolUseBlock(
                type="tool_use", id="t1", name="get_server_settings",
                input={"feature": "economy"},
            ),
            ToolUseBlock(type="tool_use", id="t2", name="find_setup_gaps", input={}),
            stop_reason="tool_use",
        ),
        _resp(TextBlock(type="text", text="Daily reward is 100.")),
    ])
    monkeypatch.setattr(advisor_service, "get_client", lambda: client)
    monkeypatch.setattr(advisor_service, "load_manual_text", lambda *a, **k: "GUIDE")

    try:
        resp = open_client.post("/api/help/advisor", json={"question": "what's set up?"})
    finally:
        fake_ctx.bot = None

    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"] == "Daily reward is 100."
    assert seen == {"fetch_settings": False, "fetch_gaps": False}, (
        "an advisor config tool ran on the event loop"
    )


# ── B-SEC9 — avatar fetch is pinned to the validated address ─────────────
#
# Validating a hostname and then handing the *hostname* to httpx leaves a
# DNS-rebinding TOCTOU: httpx resolves it a second time at connect, and a
# hostile nameserver answers the first lookup public and the second 127.0.0.1.


PUBLIC_IP = "93.184.216.34"


@contextmanager
def _mock_httpx(monkeypatch, handler):
    """Route ``_download_avatar_bytes``'s client through a MockTransport."""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    try:
        yield
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_client)


def test_reject_unsafe_avatar_url_returns_the_address_it_validated():
    from web_server.routes.config import _reject_unsafe_avatar_url

    assert _reject_unsafe_avatar_url(f"https://{PUBLIC_IP}/a.png") == PUBLIC_IP


def test_pin_url_to_ip_keeps_the_name_for_host_and_sni():
    from web_server.routes.config import _pin_url_to_ip

    connect, host_header, sni = _pin_url_to_ip("https://cdn.example.com/a.png", PUBLIC_IP)
    assert connect == f"https://{PUBLIC_IP}/a.png"
    assert host_header == "cdn.example.com"
    assert sni == "cdn.example.com"


def test_pin_url_to_ip_preserves_an_explicit_port_and_brackets_ipv6():
    from web_server.routes.config import _pin_url_to_ip

    connect, host_header, _ = _pin_url_to_ip("http://cdn.example.com:8080/a.png", PUBLIC_IP)
    assert connect == f"http://{PUBLIC_IP}:8080/a.png"
    assert host_header == "cdn.example.com:8080"

    connect6, _, _ = _pin_url_to_ip("https://cdn.example.com/a.png", "2606:2800::1")
    assert connect6 == "https://[2606:2800::1]/a.png"


async def test_avatar_fetch_connects_to_the_validated_ip_not_the_name(monkeypatch):
    """DNS rebinding: the second lookup returns loopback, and must never happen.

    ``gethostbyname`` is rigged to answer public once and loopback forever
    after. Because the request URL now carries the validated literal, there is
    no second lookup for the attacker to poison.
    """
    import socket as socket_mod

    from web_server.routes import config as config_routes

    answers = iter([PUBLIC_IP])

    def _rebinding(_host):
        return next(answers, "127.0.0.1")

    monkeypatch.setattr(socket_mod, "gethostbyname", _rebinding)

    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"avatarbytes")

    with _mock_httpx(monkeypatch, _handler):
        data = await config_routes._download_avatar_bytes("https://cdn.example.com/a.png")

    assert data == b"avatarbytes"
    assert seen[0].url.host == PUBLIC_IP
    assert seen[0].headers["host"] == "cdn.example.com"


async def test_avatar_fetch_still_revalidates_every_redirect_hop(monkeypatch):
    """The house per-hop re-validation must survive the pinning change."""
    import socket as socket_mod

    from web_server.routes import config as config_routes

    monkeypatch.setattr(socket_mod, "gethostbyname", lambda _h: PUBLIC_IP)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    with _mock_httpx(monkeypatch, _handler):
        with pytest.raises(HTTPException) as exc:
            await config_routes._download_avatar_bytes("https://cdn.example.com/a.png")
    assert exc.value.status_code == 400
    assert "disallowed" in str(exc.value.detail)


async def test_avatar_fetch_resolves_a_relative_redirect_against_the_name(monkeypatch):
    """Joining against the IP-pinned URL would silently drop the hostname."""
    import socket as socket_mod

    from web_server.routes import config as config_routes

    monkeypatch.setattr(socket_mod, "gethostbyname", lambda _h: PUBLIC_IP)

    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "/moved.png"})
        return httpx.Response(200, content=b"ok")

    with _mock_httpx(monkeypatch, _handler):
        assert await config_routes._download_avatar_bytes(
            "https://cdn.example.com/a.png"
        ) == b"ok"

    assert seen[1].url.path == "/moved.png"
    assert seen[1].headers["host"] == "cdn.example.com"


async def test_avatar_fetch_still_caps_the_body_at_8mb(monkeypatch):
    """The streamed size cap is a house pattern and must survive the change."""
    import socket as socket_mod

    from web_server.routes import config as config_routes

    monkeypatch.setattr(socket_mod, "gethostbyname", lambda _h: PUBLIC_IP)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (9 * 1024 * 1024))

    with _mock_httpx(monkeypatch, _handler):
        with pytest.raises(HTTPException) as exc:
            await config_routes._download_avatar_bytes("https://cdn.example.com/a.png")
    assert exc.value.status_code == 400
    assert "8 MB" in str(exc.value.detail)


async def test_avatar_fetch_does_not_resolve_dns_on_the_event_loop(monkeypatch):
    """``gethostbyname`` blocks, and the dashboard shares the bot's loop."""
    import socket as socket_mod

    from web_server.routes import config as config_routes

    seen: list[bool] = []

    def _resolve(_host):
        seen.append(_on_loop())
        return PUBLIC_IP

    monkeypatch.setattr(socket_mod, "gethostbyname", _resolve)

    with _mock_httpx(monkeypatch, lambda _r: httpx.Response(200, content=b"ok")):
        await config_routes._download_avatar_bytes("https://cdn.example.com/a.png")

    assert seen == [False]


# ``open_db`` and ``sqlite3`` are imported for the trace helper's typing and to
# keep the module importable without the fixtures pulling them in implicitly.
assert open_db is not None and sqlite3 is not None


# ── Flagged Messages triage queue — sentiment-feed polarity filter ────────


def _seed_mixed_sentiment(fake_ctx) -> None:
    """Three cheerful messages newer than the one negative message.

    The ordering is the point: the feed takes the most RECENT rows, so with a
    shared limit the positives would push the negative out. That is exactly
    the case the Flagged Messages panel has to survive — a good day in chat
    must not empty the queue it exists to fill.
    """
    with fake_ctx.open_db() as conn:
        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
            " content, ts, sentiment, emotion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (8000, fake_ctx.guild_id, 77, 501, "this is awful", 1785000000, -0.8,
             "anger"),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
                " content, ts, sentiment, emotion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (8100 + i, fake_ctx.guild_id, 77, 502, f"lovely {i}",
                 1785000100 + i, 0.9, "joy"),
            )


def test_sentiment_feed_negative_polarity_returns_only_negatives(
    open_client, fake_ctx
):
    _seed_mixed_sentiment(fake_ctx)
    body = open_client.get("/api/health/sentiment-feed?polarity=negative").json()
    assert [m["message_id"] for m in body["messages"]] == ["8000"]
    assert all(m["sentiment"] <= -0.5 for m in body["messages"])


def test_sentiment_feed_positive_polarity_returns_only_positives(
    open_client, fake_ctx
):
    _seed_mixed_sentiment(fake_ctx)
    body = open_client.get("/api/health/sentiment-feed?polarity=positive").json()
    assert [m["message_id"] for m in body["messages"]] == ["8102", "8101", "8100"]


def test_sentiment_feed_without_polarity_still_returns_both_sides(
    open_client, fake_ctx
):
    _seed_mixed_sentiment(fake_ctx)
    body = open_client.get("/api/health/sentiment-feed").json()
    assert {m["message_id"] for m in body["messages"]} == {
        "8000", "8100", "8101", "8102",
    }


def test_sentiment_feed_polarities_do_not_share_a_cache_entry(
    open_client, fake_ctx
):
    """A negatives-only payload served under the shared key would hide every
    positive message from the home tile, and vice versa."""
    _seed_mixed_sentiment(fake_ctx)
    open_client.get("/api/health/sentiment-feed?polarity=negative")
    open_client.get("/api/health/sentiment-feed?polarity=positive")
    open_client.get("/api/health/sentiment-feed")
    deep = {k for k in _cache_keys(fake_ctx) if k.startswith("deep:sentiment_feed")}
    assert deep == {
        "deep:sentiment_feed",
        "deep:sentiment_feed|polarity=negative",
        "deep:sentiment_feed|polarity=positive",
    }


def test_sentiment_feed_rejects_an_unknown_polarity(open_client, fake_ctx):
    """The value reaches a SQL fragment lookup, so the route must not accept
    arbitrary text — a closed set is enforced at the edge as well."""
    assert open_client.get(
        "/api/health/sentiment-feed?polarity=' OR 1=1 --"
    ).status_code == 422
