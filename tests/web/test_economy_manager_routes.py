"""Tests for /api/economy/* Bank Manager endpoints (require_economy_manager)."""

from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services import economy_qotd_sponsor_service as sponsor_svc
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services.economy_service import (
    apply_credit,
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
        # Set bonuses zeroed — one-quest boards would pay the
        # clear-the-board bonus on approval and skew exact balances.
        save_econ_settings(
            conn,
            fake_ctx.guild_id,
            {
                "enabled": True,
                "quest_set_bonus_daily": 0,
                "quest_set_bonus_weekly": 0,
            },
        )


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


def test_active_toggle(authed_client):
    # Dailies form a pool the per-user board draws from, so several can be
    # active at once (the pool cap → 409 is covered at the service layer).
    first = _make_quest(authed_client, title="Daily A")["id"]
    second = _make_quest(authed_client, title="Daily B")["id"]

    for qid in (first, second):
        resp = authed_client.post(
            f"/api/economy/quests/{qid}/active", json={"active": True}
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is True

    # Toggling one back off still works.
    resp = authed_client.post(
        f"/api/economy/quests/{first}/active", json={"active": False}
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is False


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


# ── sponsored QOTD queue ───────────────────────────────────────────────


SPONSOR_PRICE = 40
QUESTION = "What is the strangest thing you have ever eaten?"


def _seed_submission(fake_ctx, user_id: int, question: str = QUESTION) -> int:
    """Fund a member and buy one pending submission through the service."""
    with open_db(fake_ctx.db_path) as conn:
        save_econ_settings(
            conn, fake_ctx.guild_id, {"price_qotd_sponsor": SPONSOR_PRICE}
        )
        apply_credit(conn, fake_ctx.guild_id, user_id, SPONSOR_PRICE, "grant")
        settings = load_econ_settings(conn, fake_ctx.guild_id)
        outcome = sponsor_svc.submit_sponsor(
            conn, settings, fake_ctx.guild_id, user_id, question
        )
        return outcome.submission_id


def _submission(fake_ctx, sub_id: int):
    with open_db(fake_ctx.db_path) as conn:
        return sponsor_svc.get_submission(conn, sub_id)


def test_approve_queues_submission(authed_client, fake_ctx):
    sub_id = _seed_submission(fake_ctx, user_id=701)

    resp = authed_client.post(f"/api/economy/qotd-submissions/{sub_id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "approved"
    assert resp.json()["card_updated"] is False  # no bot in tests

    row = _submission(fake_ctx, sub_id)
    assert row["state"] == "approved"
    assert row["resolver_id"]  # the dashboard user, recorded for the audit
    with open_db(fake_ctx.db_path) as conn:
        # Approval keeps the money: only a decline refunds.
        assert get_balance(conn, fake_ctx.guild_id, 701) == 0
        assert sponsor_svc.next_approved(conn, fake_ctx.guild_id)["id"] == sub_id

    # Approved is no longer pending → a second approve is a 409, not a re-queue.
    assert (
        authed_client.post(
            f"/api/economy/qotd-submissions/{sub_id}/approve"
        ).status_code
        == 409
    )


def test_deny_refunds_and_requires_reason(authed_client, fake_ctx):
    sub_id = _seed_submission(fake_ctx, user_id=702)

    assert (
        authed_client.post(
            f"/api/economy/qotd-submissions/{sub_id}/deny", json={"reason": ""}
        ).status_code
        == 422
    )

    resp = authed_client.post(
        f"/api/economy/qotd-submissions/{sub_id}/deny", json={"reason": "Off topic"}
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "denied"

    row = _submission(fake_ctx, sub_id)
    assert row["deny_reason"] == "Off topic"
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 702) == SPONSOR_PRICE

    # Replay must not pay twice.
    assert (
        authed_client.post(
            f"/api/economy/qotd-submissions/{sub_id}/deny", json={"reason": "again"}
        ).status_code
        == 409
    )
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 702) == SPONSOR_PRICE


def test_withdraw_pulls_approved_back_and_refunds(authed_client, fake_ctx):
    sub_id = _seed_submission(fake_ctx, user_id=703)

    # Withdraw only applies once approved.
    assert (
        authed_client.post(
            f"/api/economy/qotd-submissions/{sub_id}/withdraw", json={"reason": ""}
        ).status_code
        == 409
    )
    authed_client.post(f"/api/economy/qotd-submissions/{sub_id}/approve")

    resp = authed_client.post(
        f"/api/economy/qotd-submissions/{sub_id}/withdraw",
        json={"reason": "Saving it for next month"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "denied"

    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 703) == SPONSOR_PRICE
        assert sponsor_svc.next_approved(conn, fake_ctx.guild_id) is None


def test_withdraw_reason_optional(authed_client, fake_ctx):
    sub_id = _seed_submission(fake_ctx, user_id=704)
    authed_client.post(f"/api/economy/qotd-submissions/{sub_id}/approve")

    resp = authed_client.post(f"/api/economy/qotd-submissions/{sub_id}/withdraw")
    assert resp.status_code == 200
    assert _submission(fake_ctx, sub_id)["deny_reason"] == ""


def test_list_submissions_filters_by_state(authed_client, fake_ctx):
    pending_id = _seed_submission(fake_ctx, user_id=705)
    approved_id = _seed_submission(fake_ctx, user_id=706)
    authed_client.post(f"/api/economy/qotd-submissions/{approved_id}/approve")

    pending = authed_client.get(
        "/api/economy/qotd-submissions", params={"state": "pending"}
    ).json()["submissions"]
    assert [s["id"] for s in pending] == [pending_id]
    assert pending[0]["user_id"] == "705"
    assert pending[0]["question"] == QUESTION
    assert pending[0]["price"] == SPONSOR_PRICE
    assert pending[0]["resolved_at"] is None

    approved = authed_client.get(
        "/api/economy/qotd-submissions", params={"state": "approved"}
    ).json()["submissions"]
    assert [s["id"] for s in approved] == [approved_id]

    everything = authed_client.get("/api/economy/qotd-submissions").json()[
        "submissions"
    ]
    assert {s["id"] for s in everything} == {pending_id, approved_id}


def test_submission_endpoints_unknown_id_404(authed_client):
    assert (
        authed_client.post("/api/economy/qotd-submissions/9999/approve").status_code
        == 404
    )


def test_submission_endpoints_require_manager(fake_ctx):
    _set_manager_role(fake_ctx)
    sub_id = _seed_submission(fake_ctx, user_id=707)
    client = _client(fake_ctx, admin=False, role_ids=[123])
    assert client.get("/api/economy/qotd-submissions").status_code == 403
    assert (
        client.post(f"/api/economy/qotd-submissions/{sub_id}/approve").status_code
        == 403
    )
    client.close()

    mgr = _client(fake_ctx, admin=False, role_ids=[MANAGER_ROLE])
    assert mgr.get("/api/economy/qotd-submissions").status_code == 200
    mgr.close()


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
        perk="role_color",
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
    assert gift["perk"] == "role_color"
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
        perk="role_color",
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


# ── statistics ──────────────────────────────────────────────────────────


def _seed_stats_wallet(fake_ctx, user_id: int, balance: int) -> None:
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO econ_wallets "
            "(guild_id, user_id, balance, created_at, updated_at) VALUES (?, ?, ?, 0, 0)",
            (fake_ctx.guild_id, user_id, balance),
        )
        conn.execute(
            "INSERT INTO econ_ledger "
            "(guild_id, user_id, amount, kind, actor_id, meta, created_at) "
            "VALUES (?, ?, 40, 'login', NULL, NULL, ?)",
            (fake_ctx.guild_id, user_id, time.time() - 3600),
        )


def test_stats_shape(authed_client, fake_ctx):
    _seed_stats_wallet(fake_ctx, 100, 500)
    _seed_stats_wallet(fake_ctx, 101, 50)
    resp = authed_client.get("/api/economy/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in (
        "supply",
        "distribution",
        "flow_7d",
        "members",
        "engagement",
        "transfers_top",
        "affordability",
    ):
        assert key in body
    assert body["supply"]["holders"] == 2
    assert len(body["members"]) == 2


def test_stats_limit_capped(authed_client, fake_ctx):
    for uid in range(600, 606):
        _seed_stats_wallet(fake_ctx, uid, uid)
    # Over-cap request is clamped to 500 (server-side); a small limit truncates.
    body = authed_client.get("/api/economy/stats", params={"limit": 2}).json()
    assert len(body["members"]) == 2
    # A huge limit is accepted (capped internally), returns all rows.
    body = authed_client.get("/api/economy/stats", params={"limit": 100000}).json()
    assert len(body["members"]) == 6


def test_stats_gated_to_manager(fake_ctx):
    _set_manager_role(fake_ctx)
    client = _client(fake_ctx, admin=False, role_ids=[123])
    assert client.get("/api/economy/stats").status_code == 403
    client.close()


# ── sponsored emojis (sinks round 3, stage 4) ─────────────────────────


def _seed_emoji_submission(fake_ctx, tmp_path, *, name="party_blob", animated=False):
    from bot_modules.services import economy_emoji_service as emoji_svc
    from bot_modules.services.economy_service import (
        apply_credit,
        load_econ_settings,
        save_econ_settings,
    )

    img = tmp_path / f"{name}.png"
    img.write_bytes(b"\x89PNG fake")
    with open_db(fake_ctx.db_path) as conn:
        save_econ_settings(conn, fake_ctx.guild_id, {"enabled": True})
        settings = load_econ_settings(conn, fake_ctx.guild_id)
        apply_credit(conn, fake_ctx.guild_id, 100, 500, "grant")
        out = emoji_svc.submit_sponsorship(
            conn, settings, fake_ctx.guild_id, 100,
            name=name, image_path=str(img), animated=animated,
            blocklist_patterns=[], taken_names=set(), guild_slots_free=True,
        )
    return out.submission_id


def test_emoji_submissions_list_and_image(authed_client, fake_ctx, tmp_path):
    sid = _seed_emoji_submission(fake_ctx, tmp_path)
    subs = authed_client.get(
        "/api/economy/emoji-submissions?state=pending"
    ).json()["submissions"]
    assert [s["id"] for s in subs] == [sid]
    assert subs[0]["name"] == "party_blob"
    assert subs[0]["user_id"] == "100"

    img = authed_client.get(f"/api/economy/emoji-submissions/{sid}/image")
    assert img.status_code == 200
    assert img.content == b"\x89PNG fake"


def test_emoji_deny_refunds_via_route(authed_client, fake_ctx, tmp_path):
    from bot_modules.services.economy_service import get_balance

    sid = _seed_emoji_submission(fake_ctx, tmp_path)
    resp = authed_client.post(
        f"/api/economy/emoji-submissions/{sid}/deny",
        json={"reason": "not a fit"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "denied"
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 100) == 500


def test_emoji_approve_uploads_and_goes_live(authed_client, fake_ctx, tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    sid = _seed_emoji_submission(fake_ctx, tmp_path)

    uploaded = MagicMock()
    uploaded.id = 424242
    guild = MagicMock()
    guild.create_custom_emoji = AsyncMock(return_value=uploaded)

    class _ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, _guild_id):
            return guild

    fake_ctx.bot = _ReadyBot()
    try:
        resp = authed_client.post(f"/api/economy/emoji-submissions/{sid}/approve")
    finally:
        fake_ctx.bot = None
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "state": "live"}
    guild.create_custom_emoji.assert_awaited_once()
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM econ_emoji_submissions WHERE id = ?", (sid,)
        ).fetchone()
        rental = conn.execute(
            "SELECT * FROM econ_rentals WHERE id = ?", (row["rental_id"],)
        ).fetchone()
    assert row["emoji_id"] == 424242
    assert rental["perk"] == "emoji" and rental["state"] == "active"


def test_emoji_approve_upload_failure_denies_and_refunds(
    authed_client, fake_ctx, tmp_path
):
    from unittest.mock import AsyncMock, MagicMock

    from bot_modules.services.economy_service import get_balance

    sid = _seed_emoji_submission(fake_ctx, tmp_path)
    guild = MagicMock()
    guild.create_custom_emoji = AsyncMock(side_effect=RuntimeError("no slots"))

    class _ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, _guild_id):
            return guild

    fake_ctx.bot = _ReadyBot()
    try:
        resp = authed_client.post(f"/api/economy/emoji-submissions/{sid}/approve")
    finally:
        fake_ctx.bot = None
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["state"] == "denied"
    with open_db(fake_ctx.db_path) as conn:
        assert get_balance(conn, fake_ctx.guild_id, 100) == 500  # refunded


def test_emoji_approve_requires_connected_bot(authed_client, fake_ctx, tmp_path):
    sid = _seed_emoji_submission(fake_ctx, tmp_path)
    resp = authed_client.post(f"/api/economy/emoji-submissions/{sid}/approve")
    assert resp.status_code == 503
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT state FROM econ_emoji_submissions WHERE id = ?", (sid,)
        ).fetchone()
    assert row["state"] == "pending"  # no claim burned
