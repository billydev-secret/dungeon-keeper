"""Tests for /api/economy/* Bank Manager endpoints (require_economy_manager)."""

from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services.economy_service import (
    get_balance,
    load_econ_settings,
    save_econ_settings,
)
from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
from web_server.server import create_app

MANAGER_ROLE = 9999


def _client(fake_ctx, *, admin: bool, role_ids: list[int] | None = None) -> TestClient:
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(
        create_app(fake_ctx, auth=auth), raise_server_exceptions=False
    )
    cookie = auth.create_session_cookie(
        user_id=42,
        username="mgr",
        access_token="token",
        permission_bits=0x8 if admin else 0,
        role_ids=role_ids or [],
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


def _set_manager_role(fake_ctx, role_id: int = MANAGER_ROLE) -> None:
    with open_db(fake_ctx.db_path) as conn:
        save_econ_settings(conn, fake_ctx.guild_id, {"manager_role_id": role_id})


def _enable_economy(fake_ctx) -> None:
    with open_db(fake_ctx.db_path) as conn:
        save_econ_settings(conn, fake_ctx.guild_id, {"enabled": True})


def _make_quest(client, **overrides) -> dict:
    body = {
        "title": "Say hi",
        "description": "Greet someone",
        "qtype": "daily",
        "reward": 15,
        "signoff": False,
        "criteria": "Post a greeting",
    }
    body.update(overrides)
    resp = client.post("/api/economy/quests", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── gating matrix ──────────────────────────────────────────────────────


def test_admin_can_list(authed_client):
    # authed_client is an admin (permission_bits 0x8).
    assert authed_client.get("/api/economy/quests").status_code == 200


def test_manager_role_can_list(fake_ctx):
    _set_manager_role(fake_ctx)
    client = _client(fake_ctx, admin=False, role_ids=[MANAGER_ROLE])
    assert client.get("/api/economy/quests").status_code == 200
    client.close()


def test_plain_member_forbidden(fake_ctx):
    _set_manager_role(fake_ctx)
    client = _client(fake_ctx, admin=False, role_ids=[123])
    assert client.get("/api/economy/quests").status_code == 403
    client.close()


def test_me_exposes_manager_role_id(authed_client, fake_ctx):
    assert authed_client.get("/api/me").json()["economy_manager_role_id"] is None
    _set_manager_role(fake_ctx, 4242)
    assert (
        authed_client.get("/api/me").json()["economy_manager_role_id"] == "4242"
    )


# ── quest CRUD ─────────────────────────────────────────────────────────


def test_quest_crud_roundtrip(authed_client):
    created = _make_quest(authed_client, title="Original")
    qid = created["id"]
    assert created["active"] is False
    assert created["reward"] == 15

    # list
    listing = authed_client.get("/api/economy/quests").json()["quests"]
    assert any(q["id"] == qid for q in listing)

    # update
    resp = authed_client.put(
        f"/api/economy/quests/{qid}", json={"title": "Renamed", "reward": 18}
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
    assert resp.json()["reward"] == 18

    # delete (no paid claims → ok)
    assert authed_client.delete(f"/api/economy/quests/{qid}").status_code == 200
    assert authed_client.get("/api/economy/quests").json()["quests"] == []


def test_update_unknown_field_422(authed_client):
    qid = _make_quest(authed_client)["id"]
    # `active` is not an accepted update field (extra="forbid").
    resp = authed_client.put(f"/api/economy/quests/{qid}", json={"active": True})
    assert resp.status_code == 422


def test_create_reward_band_not_enforced(authed_client):
    # Reward well outside the daily band (10–20) still saves fine.
    created = _make_quest(authed_client, qtype="daily", reward=500)
    assert created["reward"] == 500


# ── active toggle + slot rule ──────────────────────────────────────────


def test_active_toggle_and_slot_limit(authed_client):
    first = _make_quest(authed_client, title="Daily A")["id"]
    second = _make_quest(authed_client, title="Daily B")["id"]

    resp = authed_client.post(
        f"/api/economy/quests/{first}/active", json={"active": True}
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is True

    # Second daily activation exceeds the ≤1 daily slot rule → 409.
    resp = authed_client.post(
        f"/api/economy/quests/{second}/active", json={"active": True}
    )
    assert resp.status_code == 409


# ── delete with paid claims ────────────────────────────────────────────


def _seed_paid_claim(fake_ctx, quest_id: int, user_id: int) -> None:
    with open_db(fake_ctx.db_path) as conn:
        settings = load_econ_settings(conn, fake_ctx.guild_id)
        quests_svc.set_quest_active(conn, fake_ctx.guild_id, quest_id, True)
        quests_svc.claim_quest(
            conn,
            settings,
            fake_ctx.guild_id,
            quest_id,
            user_id,
            period="2026-07-10",
            booster=False,
        )


def test_delete_with_paid_claims_409(authed_client, fake_ctx):
    qid = _make_quest(authed_client, signoff=False, reward=10)["id"]
    _seed_paid_claim(fake_ctx, qid, user_id=777)
    resp = authed_client.delete(f"/api/economy/quests/{qid}")
    assert resp.status_code == 409


# ── claims: approve / deny ─────────────────────────────────────────────


def _seed_pending_claim(fake_ctx, quest_id: int, user_id: int, period: str) -> int:
    with open_db(fake_ctx.db_path) as conn:
        settings = load_econ_settings(conn, fake_ctx.guild_id)
        quests_svc.set_quest_active(conn, fake_ctx.guild_id, quest_id, True)
        outcome = quests_svc.claim_quest(
            conn,
            settings,
            fake_ctx.guild_id,
            quest_id,
            user_id,
            period=period,
            booster=False,
        )
        assert outcome.state == "pending"
        return outcome.claim_id


def test_approve_pays_and_flips_state(authed_client, fake_ctx):
    qid = _make_quest(authed_client, signoff=True, reward=20)["id"]
    claim_id = _seed_pending_claim(fake_ctx, qid, user_id=555, period="2026-07-10")

    resp = authed_client.post(f"/api/economy/claims/{claim_id}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid"] == 20
    assert body["card_updated"] is False  # no bot in tests

    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 555) == 20
        row = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
    assert row["state"] == "paid"

    # Already-resolved second approve → 409.
    assert (
        authed_client.post(f"/api/economy/claims/{claim_id}/approve").status_code
        == 409
    )


def test_deny_requires_reason_and_reclaimable(authed_client, fake_ctx):
    qid = _make_quest(authed_client, signoff=True, reward=20)["id"]
    claim_id = _seed_pending_claim(fake_ctx, qid, user_id=556, period="2026-07-10")

    # Empty reason → 422.
    assert (
        authed_client.post(
            f"/api/economy/claims/{claim_id}/deny", json={"reason": ""}
        ).status_code
        == 422
    )

    resp = authed_client.post(
        f"/api/economy/claims/{claim_id}/deny", json={"reason": "Not enough detail"}
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT state, deny_reason FROM econ_quest_claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
        assert row["state"] == "denied"
        assert row["deny_reason"] == "Not enough detail"
        # Member can re-claim the same period (a new pending row).
        settings = load_econ_settings(conn, fake_ctx.guild_id)
        again = quests_svc.claim_quest(
            conn,
            settings,
            fake_ctx.guild_id,
            qid,
            556,
            period="2026-07-10",
            booster=False,
        )
        assert again.state == "pending"

    # Second deny on the resolved claim → 409.
    assert (
        authed_client.post(
            f"/api/economy/claims/{claim_id}/deny", json={"reason": "again"}
        ).status_code
        == 409
    )


def test_list_pending_claims(authed_client, fake_ctx):
    qid = _make_quest(authed_client, signoff=True, reward=5)["id"]
    _seed_pending_claim(fake_ctx, qid, user_id=600, period="2026-07-10")
    claims = authed_client.get(
        "/api/economy/claims", params={"state": "pending"}
    ).json()["claims"]
    assert len(claims) == 1
    assert claims[0]["user_id"] == "600"
    assert claims[0]["deny_count"] == 0


# ── community progress + settle ────────────────────────────────────────


def _seed_active_members(fake_ctx, user_ids: list[int]) -> None:
    now = time.time()
    with open_db(fake_ctx.db_path) as conn:
        for uid in user_ids:
            conn.execute(
                """
                INSERT INTO member_activity
                    (guild_id, user_id, last_channel_id, last_message_id, last_message_at)
                VALUES (?, ?, 1, 1, ?)
                """,
                (fake_ctx.guild_id, uid, now),
            )


def test_progress_and_settle_idempotent(authed_client, fake_ctx):
    qid = _make_quest(
        authed_client, qtype="community", reward=30, community_target=100
    )["id"]
    _seed_active_members(fake_ctx, [111, 222])

    # Progress below then reaching target.
    r = authed_client.post(
        f"/api/economy/quests/{qid}/progress", json={"current": 50}
    )
    assert r.json()["completed"] is False
    r = authed_client.post(
        f"/api/economy/quests/{qid}/progress", json={"current": 100}
    )
    assert r.json()["completed"] is True

    # First settle pays both members.
    r = authed_client.post(f"/api/economy/quests/{qid}/settle")
    assert r.status_code == 200
    assert r.json()["paid_count"] == 2

    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 111) == 30
        assert get_balance(conn, fake_ctx.guild_id, 222) == 30

    # Second settle is idempotent — pays 0 new.
    r = authed_client.post(f"/api/economy/quests/{qid}/settle")
    assert r.json()["paid_count"] == 0


def test_settle_rejected_for_non_community(authed_client):
    qid = _make_quest(authed_client, qtype="daily")["id"]
    resp = authed_client.post(f"/api/economy/quests/{qid}/settle")
    assert resp.status_code == 422


def test_progress_rejected_for_non_community(authed_client):
    qid = _make_quest(authed_client, qtype="daily")["id"]
    resp = authed_client.post(
        f"/api/economy/quests/{qid}/progress", json={"current": 5}
    )
    assert resp.status_code == 422


# ── grant + ledger ─────────────────────────────────────────────────────


def test_grant_credits_and_ledgers(authed_client, fake_ctx):
    _enable_economy(fake_ctx)
    resp = authed_client.post(
        "/api/economy/grant",
        json={"member_id": 314, "amount": 42, "reason": "mvp"},
    )
    assert resp.status_code == 200
    assert resp.json()["credited"] == 42
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 314) == 42


def test_grant_rejects_zero_amount(authed_client):
    resp = authed_client.post(
        "/api/economy/grant", json={"member_id": 1, "amount": 0, "reason": ""}
    )
    assert resp.status_code == 422


def test_grant_refused_when_economy_disabled(authed_client, fake_ctx):
    # Economy is disabled by default; grant must mirror /bank grant's gate.
    resp = authed_client.post(
        "/api/economy/grant",
        json={"member_id": 314, "amount": 42, "reason": "mvp"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "economy disabled"
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 314) == 0


def test_ledger_filters_and_cap(authed_client, fake_ctx):
    _enable_economy(fake_ctx)
    authed_client.post(
        "/api/economy/grant", json={"member_id": 10, "amount": 5, "reason": "a"}
    )
    authed_client.post(
        "/api/economy/grant", json={"member_id": 20, "amount": 7, "reason": "b"}
    )

    # user filter
    entries = authed_client.get(
        "/api/economy/ledger", params={"user_id": 10}
    ).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["user_id"] == "10"
    assert entries[0]["kind"] == "grant"

    # kind filter (no matching kind → empty)
    entries = authed_client.get(
        "/api/economy/ledger", params={"kind": "quest"}
    ).json()["entries"]
    assert entries == []

    # limit is capped at 500 (over-large request doesn't error)
    assert (
        authed_client.get("/api/economy/ledger", params={"limit": 100000}).status_code
        == 200
    )


# ── perk rentals ───────────────────────────────────────────────────────


def _seed_rental(
    fake_ctx,
    *,
    user_id: int,
    perk: str = "role_color",
    state: str = "active",
    price: int = 50,
    beneficiary_id: int | None = None,
    next_bill_at: float | None = None,
) -> int:
    """Insert a rental row directly (bypasses the upfront charge) so tests can
    put a rental into any state. Callers vary (user_id, perk, beneficiary) per
    live row to respect the one-live-rental unique index."""
    now = time.time()
    beneficiary = user_id if beneficiary_id is None else beneficiary_id
    nb = now + 7 * 86400 if next_bill_at is None else next_bill_at
    with open_db(fake_ctx.db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at,
                 next_bill_at, beneficiary_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fake_ctx.guild_id, user_id, perk, state, price, now, nb, beneficiary, now),
        )
        return int(cur.lastrowid or 0)


def _rental_state(fake_ctx, rental_id: int) -> sqlite3.Row:
    with open_db(fake_ctx.db_path) as conn:
        return conn.execute(
            "SELECT state, cancel_at_period_end FROM econ_rentals WHERE id = ?",
            (rental_id,),
        ).fetchone()


def test_list_rentals_shape(authed_client, fake_ctx):
    # An active self-rental and a gifted (beneficiary ≠ owner) grace-state one.
    _seed_rental(fake_ctx, user_id=100, perk="role_color", state="active", price=50)
    gift_id = _seed_rental(
        fake_ctx,
        user_id=200,
        perk="gift_color",
        state="grace",
        price=60,
        beneficiary_id=201,
    )
    # A lapsed rental must NOT appear (default states = active + grace only).
    _seed_rental(fake_ctx, user_id=300, perk="role_icon", state="lapsed")

    rentals = authed_client.get("/api/economy/rentals").json()["rentals"]
    assert len(rentals) == 2
    by_id = {r["id"]: r for r in rentals}
    gift = by_id[gift_id]
    assert gift["user_id"] == "200"
    assert gift["beneficiary_id"] == "201"
    assert gift["perk"] == "gift_color"
    assert gift["state"] == "grace"
    assert gift["price"] == 60
    assert gift["suspended"] is False
    assert gift["cancel_at_period_end"] is False
    assert "next_bill_at" in gift


def test_list_rentals_reports_suspended(authed_client, fake_ctx):
    rid = _seed_rental(fake_ctx, user_id=100, perk="role_icon", state="active")
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "UPDATE econ_rentals SET suspended = 1, suspended_since = ? WHERE id = ?",
            (time.time(), rid),
        )
    rentals = authed_client.get("/api/economy/rentals").json()["rentals"]
    assert rentals[0]["suspended"] is True


def test_cancel_active_marks_period_end(authed_client, fake_ctx):
    rid = _seed_rental(fake_ctx, user_id=100, perk="role_color", state="active")
    resp = authed_client.post(f"/api/economy/rentals/{rid}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "active"  # stays live until the anniversary tick
    assert body["cancel_at_period_end"] is True
    row = _rental_state(fake_ctx, rid)
    assert row["state"] == "active"
    assert row["cancel_at_period_end"] == 1


def test_cancel_grace_cancels_immediately(authed_client, fake_ctx):
    rid = _seed_rental(fake_ctx, user_id=100, perk="role_color", state="grace")
    resp = authed_client.post(f"/api/economy/rentals/{rid}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "cancelled"
    # No bot in tests → the de-projection is skipped and reported false, but the
    # state change still lands.
    assert body["role_updated"] is False
    assert _rental_state(fake_ctx, rid)["state"] == "cancelled"


def test_cancel_active_reports_role_updated_false(authed_client, fake_ctx):
    # An active cancel only sets cancel_at_period_end — no de-projection.
    rid = _seed_rental(fake_ctx, user_id=100, perk="role_color", state="active")
    body = authed_client.post(f"/api/economy/rentals/{rid}/cancel").json()
    assert body["role_updated"] is False


def test_cancel_grace_deprojects_with_ready_bot(authed_client, fake_ctx):
    """A grace cancel with a ready bot runs the best-effort de-projection.

    The fake bot's ``get_guild`` returns None, so ``revoke_role_perks`` is a
    no-op that still completes — the branch runs and ``role_updated`` is True.
    A gifted rental confirms the beneficiary (not the payer) is targeted.
    """

    class _ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, _guild_id):
            return None

    rid = _seed_rental(
        fake_ctx,
        user_id=200,
        perk="gift_color",
        state="grace",
        beneficiary_id=201,
    )
    fake_ctx.bot = _ReadyBot()
    try:
        body = authed_client.post(f"/api/economy/rentals/{rid}/cancel").json()
    finally:
        fake_ctx.bot = None
    assert body["state"] == "cancelled"
    assert body["role_updated"] is True
    assert _rental_state(fake_ctx, rid)["state"] == "cancelled"


def test_cancel_unknown_rental_409(authed_client):
    assert authed_client.post("/api/economy/rentals/999999/cancel").status_code == 409


def test_cancel_lapsed_rental_409(authed_client, fake_ctx):
    rid = _seed_rental(fake_ctx, user_id=100, perk="role_color", state="lapsed")
    assert authed_client.post(f"/api/economy/rentals/{rid}/cancel").status_code == 409


def test_rentals_gated_to_manager(fake_ctx):
    _set_manager_role(fake_ctx)
    rid = _seed_rental(fake_ctx, user_id=100, perk="role_color", state="active")
    client = _client(fake_ctx, admin=False, role_ids=[123])
    assert client.get("/api/economy/rentals").status_code == 403
    assert client.post(f"/api/economy/rentals/{rid}/cancel").status_code == 403
    client.close()
