"""Tests for /api/qa/* — the QA Tracker dashboard surface (admin-only)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.services import qa_service
from bot_modules.services.economy_service import apply_debit, get_balance
from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
from web_server.server import create_app

GID = 123  # FakeCtx default guild
TESTER = 1001
OTHER = 1002
ADMIN_UID = 1  # authed_client's session user_id

S = qa_service.DEFAULT_QA_SETTINGS
DAY = local_day_for(time.time(), 0.0)


def _client(fake_ctx, *, admin: bool) -> TestClient:
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth), raise_server_exceptions=False)
    cookie = auth.create_session_cookie(
        user_id=ADMIN_UID,
        username="qa-admin",
        access_token="token",
        permission_bits=0x8 if admin else 0,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


def _seed_test(fake_ctx, n: int = 1, *, guild_id: int = GID, message: bool = False) -> int:
    with open_db(fake_ctx.db_path) as conn:
        tid = qa_service.create_test(
            conn,
            guild_id,
            f"Entry {n}",
            f"Entry {n} title",
            "- [ ] check the thing",
            commit_sha=f"abc{n:04d}",
            commit_subject=f"Feature {n}",
        )
        if message:
            qa_service.set_test_message(conn, tid, 555, 999)
        return tid


def _record(fake_ctx, tid: int, user_id: int, verdict: str, *, guild_id: int = GID,
            note: str | None = None) -> qa_service.VerdictOutcome:
    with open_db(fake_ctx.db_path) as conn:
        return qa_service.record_verdict(
            conn, S, tid, guild_id, user_id, verdict, note, local_day=DAY
        )


class _CardChannel:
    """Just enough TextChannel for the card re-render; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self) -> None:
        self.id = 555
        self.message = SimpleNamespace(edit=AsyncMock())
        self.fetch_message = AsyncMock(return_value=self.message)


def _wire_bot(fake_ctx) -> _CardChannel:
    channel = _CardChannel()
    fake_ctx.bot = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda cid: channel if cid == 555 else None,
        # The OAuth backend probes the bot's guild cache for live role
        # refresh; None makes it fall back to the session's cookie perms.
        get_guild=lambda gid: None,
    )
    return channel


# ── gating ────────────────────────────────────────────────────────────


def test_non_admin_forbidden_everywhere(fake_ctx):
    client = _client(fake_ctx, admin=False)
    assert client.get("/api/qa/tests").status_code == 403
    assert client.get("/api/qa/settings").status_code == 403
    assert client.put("/api/qa/settings", json={}).status_code == 403
    assert client.post("/api/qa/verdicts/1/void").status_code == 403
    assert client.post("/api/qa/tests/1/archive").status_code == 403
    assert client.get("/api/qa/top-testers").status_code == 403
    client.close()


# ── settings ──────────────────────────────────────────────────────────


def test_settings_defaults(authed_client):
    r = authed_client.get("/api/qa/settings")
    assert r.status_code == 200
    assert r.json() == {
        "enabled": True,
        "role_id": "0",
        "channel_id": "0",
        "reward": 15,
        "daily_cap": 4,
    }


def test_settings_roundtrip(authed_client, fake_ctx):
    big_role = 1469123456789012345  # snowflake beyond JS float precision
    body = {
        "enabled": False,
        "role_id": big_role,
        "channel_id": 777,
        "reward": 25,
        "daily_cap": 2,
    }
    r = authed_client.put("/api/qa/settings", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["enabled"] is False
    assert saved["role_id"] == str(big_role)  # string survives full-precision
    assert saved["reward"] == 25
    assert authed_client.get("/api/qa/settings").json() == saved
    # The cog-side loader sees the same values (no restart needed).
    with open_db(fake_ctx.db_path) as conn:
        settings = qa_service.load_qa_settings(conn, GID)
    assert settings.role_id == big_role
    assert settings.daily_cap == 2
    assert settings.enabled is False


def test_settings_accepts_string_snowflakes_exactly(authed_client, fake_ctx):
    """The panel sends ids as JSON strings — a JS Number would round a
    19-digit snowflake's tail off (this actually happened: …549 became …500).
    Pydantic must coerce the string to the exact int."""
    body = {
        "enabled": True,
        "role_id": "1527441929904717834",
        "channel_id": "1527184897775763549",
        "reward": 15,
        "daily_cap": 50,
    }
    assert authed_client.put("/api/qa/settings", json=body).status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        settings = qa_service.load_qa_settings(conn, GID)
    assert settings.role_id == 1527441929904717834
    assert settings.channel_id == 1527184897775763549


def test_settings_rejects_bad_payloads(authed_client):
    good = {"enabled": True, "role_id": 0, "channel_id": 0, "reward": 15, "daily_cap": 4}
    assert authed_client.put(
        "/api/qa/settings", json={**good, "bogus_key": 1}
    ).status_code == 422
    assert authed_client.put(
        "/api/qa/settings", json={**good, "reward": -1}
    ).status_code == 422
    assert authed_client.put(
        "/api/qa/settings", json={**good, "reward": 999_999}
    ).status_code == 422
    assert authed_client.put(
        "/api/qa/settings", json={**good, "role_id": -5}
    ).status_code == 422
    assert authed_client.put(
        "/api/qa/settings", json={**good, "daily_cap": 100_000}
    ).status_code == 422


# ── board ─────────────────────────────────────────────────────────────


def test_list_tests_folds_verdicts_and_jump_link(authed_client, fake_ctx):
    t1 = _seed_test(fake_ctx, 1, message=True)
    t2 = _seed_test(fake_ctx, 2)  # no message ids → no jump link
    _seed_test(fake_ctx, 3, guild_id=GID + 1)  # other guild, invisible
    _record(fake_ctx, t1, TESTER, "pass")
    _record(fake_ctx, t1, OTHER, "fail", note="broke on mobile")

    r = authed_client.get("/api/qa/tests")
    assert r.status_code == 200
    tests = r.json()["tests"]
    assert [t["id"] for t in tests] == [t2, t1]  # newest first, guild-scoped

    card = next(t for t in tests if t["id"] == t1)
    assert card["status"] == "failed"
    assert card["commit_sha"] == "abc0001"
    assert card["jump_url"] == f"https://discord.com/channels/{GID}/555/999"
    assert len(card["verdicts"]) == 2
    v_pass = next(v for v in card["verdicts"] if v["verdict"] == "pass")
    assert v_pass["user_id"] == str(TESTER)
    assert v_pass["paid_amount"] == S.reward
    assert v_pass["voided"] is False
    assert v_pass["created_at"]
    v_fail = next(v for v in card["verdicts"] if v["verdict"] == "fail")
    assert v_fail["note"] == "broke on mobile"

    bare = next(t for t in tests if t["id"] == t2)
    assert bare["jump_url"] is None
    assert bare["verdicts"] == []


def test_list_tests_status_filter(authed_client, fake_ctx):
    t1 = _seed_test(fake_ctx, 1)
    t2 = _seed_test(fake_ctx, 2)
    _record(fake_ctx, t2, TESTER, "pass")

    passed = authed_client.get("/api/qa/tests", params={"status": "passed"}).json()
    assert [t["id"] for t in passed["tests"]] == [t2]
    pending = authed_client.get("/api/qa/tests", params={"status": "pending"}).json()
    assert [t["id"] for t in pending["tests"]] == [t1]
    assert authed_client.get(
        "/api/qa/tests", params={"status": "bogus"}
    ).status_code == 422


# ── void ──────────────────────────────────────────────────────────────


def test_void_claws_back_and_recomputes(authed_client, fake_ctx):
    tid = _seed_test(fake_ctx)
    out = _record(fake_ctx, tid, TESTER, "pass")  # pays 15
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, GID, TESTER) == S.reward

    r = authed_client.post(f"/api/qa/verdicts/{out.verdict_id}/void")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["clawed"] == S.reward
    assert body["shortfall"] == 0
    assert body["status"] == "pending"
    assert body["card_updated"] is False  # no bot wired → skipped, not fatal

    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, GID, TESTER) == 0
        row = qa_service.list_verdicts(conn, tid)[0]
        assert row["voided_at"] is not None
        assert row["voided_by"] == ADMIN_UID  # the acting dashboard user
        test = qa_service.get_test(conn, tid)
        assert test is not None
        assert test["status"] == "pending"

    # Double-void is a 404 (service no-op → None).
    assert authed_client.post(f"/api/qa/verdicts/{out.verdict_id}/void").status_code == 404


def test_void_reports_shortfall_from_spent_wallet(authed_client, fake_ctx):
    tid = _seed_test(fake_ctx)
    out = _record(fake_ctx, tid, TESTER, "pass")
    with open_db(fake_ctx.db_path) as conn:
        assert apply_debit(conn, GID, TESTER, 9, "spend") is True  # balance 6

    body = authed_client.post(f"/api/qa/verdicts/{out.verdict_id}/void").json()
    assert body["clawed"] == 6
    assert body["shortfall"] == 9


def test_void_unknown_and_cross_guild_404(authed_client, fake_ctx):
    assert authed_client.post("/api/qa/verdicts/999/void").status_code == 404
    other_tid = _seed_test(fake_ctx, 7, guild_id=GID + 1)
    out = _record(fake_ctx, other_tid, TESTER, "pass", guild_id=GID + 1)
    assert authed_client.post(f"/api/qa/verdicts/{out.verdict_id}/void").status_code == 404
    with open_db(fake_ctx.db_path) as conn:  # untouched
        assert qa_service.list_verdicts(conn, other_tid)[0]["voided_at"] is None


def test_void_rerenders_card_through_bot(authed_client, fake_ctx):
    tid = _seed_test(fake_ctx, message=True)
    out = _record(fake_ctx, tid, TESTER, "pass")
    channel = _wire_bot(fake_ctx)

    body = authed_client.post(f"/api/qa/verdicts/{out.verdict_id}/void").json()
    assert body["card_updated"] is True
    channel.fetch_message.assert_awaited_once_with(999)
    channel.message.edit.assert_awaited_once()
    kwargs = channel.message.edit.await_args.kwargs
    # Only pass remained and it's voided → back to pending grey.
    assert kwargs["embed"].colour.value == 0x95A5A6
    assert "view" not in kwargs  # buttons untouched on a void


# ── archive ───────────────────────────────────────────────────────────


def test_archive_sets_status_and_404s(authed_client, fake_ctx):
    tid = _seed_test(fake_ctx)
    r = authed_client.post(f"/api/qa/tests/{tid}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"
    with open_db(fake_ctx.db_path) as conn:
        test = qa_service.get_test(conn, tid)
        assert test is not None
        assert test["status"] == "archived"
    assert authed_client.post("/api/qa/tests/999/archive").status_code == 404
    other = _seed_test(fake_ctx, 8, guild_id=GID + 1)
    assert authed_client.post(f"/api/qa/tests/{other}/archive").status_code == 404


def test_archive_strips_card_components(authed_client, fake_ctx):
    tid = _seed_test(fake_ctx, message=True)
    channel = _wire_bot(fake_ctx)

    body = authed_client.post(f"/api/qa/tests/{tid}/archive").json()
    assert body["card_updated"] is True
    kwargs = channel.message.edit.await_args.kwargs
    assert kwargs["view"] is None  # components removed
    assert kwargs["embed"].colour.value == 0x7F8C8D  # archived dark grey


def test_card_failure_never_rolls_back(authed_client, fake_ctx):
    tid = _seed_test(fake_ctx, message=True)
    channel = _wire_bot(fake_ctx)
    channel.fetch_message.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="gone"), "message deleted"
    )
    r = authed_client.post(f"/api/qa/tests/{tid}/archive")
    assert r.status_code == 200
    assert r.json()["card_updated"] is False
    with open_db(fake_ctx.db_path) as conn:
        test = qa_service.get_test(conn, tid)
        assert test is not None
        assert test["status"] == "archived"  # DB change stands


# ── top testers ───────────────────────────────────────────────────────


def test_top_testers_aggregation(authed_client, fake_ctx):
    assert authed_client.get("/api/qa/top-testers").json() == {"testers": []}

    t1 = _seed_test(fake_ctx, 1)
    t2 = _seed_test(fake_ctx, 2)
    t3 = _seed_test(fake_ctx, 3)
    _record(fake_ctx, t1, TESTER, "pass")  # +15
    _record(fake_ctx, t2, TESTER, "pass")  # +15
    voided = _record(fake_ctx, t1, OTHER, "fail", note="x")  # +15
    _record(fake_ctx, t3, OTHER, "blocked", note="env")  # +15
    with open_db(fake_ctx.db_path) as conn:
        assert qa_service.void_verdict(conn, voided.verdict_id, ADMIN_UID) is not None

    testers = authed_client.get("/api/qa/top-testers").json()["testers"]
    assert testers == [
        # Voided verdicts drop out of the count; coins stay gross (the
        # clawback is a separate qa_void debit).
        {"user_id": str(TESTER), "verdicts": 2, "coins": 30},
        {"user_id": str(OTHER), "verdicts": 1, "coins": 30},
    ]
