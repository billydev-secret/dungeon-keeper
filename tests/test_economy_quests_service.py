"""Tests for services/economy_quests_service.py — the claim state machine.

Covers the money-critical paths: instant pay-once-per-period across day
boundaries, the pending/paid uniqueness races (asserted via direct duplicate
inserts, not just the public API), the deny → re-claim → approve chain, the
approve-time double-pay backstop, one-shot expiry transitions, community
settlement idempotency and crash-replay, slot-limit errors, rotation, and
delete refusal on paid claims.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.quests import occurrence_period
from bot_modules.services.economy_quests_service import (
    ClaimOutcome,
    SlotLimitError,
    active_member_ids,
    claim_quest,
    create_quest,
    delete_quest,
    deny_history,
    list_claims,
    list_settleable_community_quests,
    list_trigger_quests,
    expire_stale_claims,
    fire_trigger_inline,
    fire_trigger_quests,
    get_photo_card,
    get_quest,
    list_income_sources,
    list_kind_triggered_quests,
    list_quests,
    record_photo_card,
    set_income_source,
    source_enabled,
    resolve_claim,
    rotate_pool,
    set_claim_card,
    set_community_progress,
    set_quest_active,
    settle_community_quest,
    update_quest,
)
from bot_modules.services.economy_service import (
    EconSettings,
    get_balance,
    save_econ_settings,
)
from migrations import apply_migrations_sync

GUILD = 500
USER = 1001
OTHER = 1002
MANAGER = 9001

SETTINGS = EconSettings(enabled=True, booster_multiplier=1.5)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _make(
    conn,
    *,
    qtype="daily",
    reward=10,
    signoff=0,
    active=True,
    rotate_tag="",
    community_target=None,
    starts_at=None,
    ends_at=None,
    guild_id=GUILD,
    trigger_words="",
    trigger_channel_id=None,
    trigger_kind="",
):
    qid = create_quest(
        conn,
        guild_id,
        title="Quest",
        description="desc",
        qtype=qtype,
        reward=reward,
        signoff=signoff,
        criteria="do the thing",
        starts_at=starts_at,
        ends_at=ends_at,
        rotate_tag=rotate_tag,
        community_target=community_target,
        created_by=MANAGER,
        trigger_words=trigger_words,
        trigger_channel_id=trigger_channel_id,
        trigger_kind=trigger_kind,
    )
    if active:
        set_quest_active(conn, guild_id, qid, True)
    return qid


def _get(conn, guild_id, quest_id):
    row = get_quest(conn, guild_id, quest_id)
    assert row is not None
    return row


# ── CRUD ──────────────────────────────────────────────────────────────


def test_create_get_list(db):
    with open_db(db) as conn:
        qid = _make(conn, active=False)
        row = get_quest(conn, GUILD, qid)
        assert row is not None
        assert row["title"] == "Quest"
        assert row["active"] == 0
        assert row["qtype"] == "daily"
        assert list_quests(conn, GUILD) != []
        assert list_quests(conn, GUILD, active_only=True) == []


def test_create_unknown_type_raises(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            create_quest(
                conn,
                GUILD,
                title="x",
                description="",
                qtype="monthly",
                reward=1,
                signoff=0,
                criteria="",
                starts_at=None,
                ends_at=None,
                rotate_tag="",
                community_target=None,
                created_by=1,
            )


def test_get_quest_wrong_guild_is_none(db):
    with open_db(db) as conn:
        qid = _make(conn)
        assert get_quest(conn, GUILD + 1, qid) is None


def test_update_quest_patches_fields(db):
    with open_db(db) as conn:
        qid = _make(conn)
        update_quest(conn, GUILD, qid, {"title": "New", "reward": 42})
        row = get_quest(conn, GUILD, qid)
        assert row is not None
        assert row["title"] == "New"
        assert row["reward"] == 42


def test_update_quest_unknown_field_raises(db):
    with open_db(db) as conn:
        qid = _make(conn)
        with pytest.raises(KeyError):
            update_quest(conn, GUILD, qid, {"bogus": 1})


def test_update_quest_empty_noop(db):
    with open_db(db) as conn:
        qid = _make(conn)
        update_quest(conn, GUILD, qid, {})  # must not raise


def test_update_quest_cannot_bypass_slot_rule_via_active(db):
    # ``active`` is not an updatable field — activation must go through the
    # slot-checked set_quest_active path.
    with open_db(db) as conn:
        qid = _make(conn, active=False)
        with pytest.raises(KeyError):
            update_quest(conn, GUILD, qid, {"active": 1})


# ── slot rule ─────────────────────────────────────────────────────────


def test_second_active_daily_raises_slot_limit(db):
    with open_db(db) as conn:
        _make(conn, qtype="daily")
        q2 = _make(conn, qtype="daily", active=False)
        with pytest.raises(SlotLimitError):
            set_quest_active(conn, GUILD, q2, True)


def test_sixth_active_weekly_raises_slot_limit(db):
    with open_db(db) as conn:
        for _ in range(5):
            _make(conn, qtype="weekly")
        q6 = _make(conn, qtype="weekly", active=False)
        with pytest.raises(SlotLimitError):
            set_quest_active(conn, GUILD, q6, True)


def test_slot_limit_is_per_guild(db):
    with open_db(db) as conn:
        _make(conn, qtype="daily", guild_id=GUILD)
        # a daily in another guild does not consume this guild's slot
        q2 = _make(conn, qtype="daily", active=False, guild_id=GUILD + 1)
        set_quest_active(conn, GUILD + 1, q2, True)  # no raise
        assert _get(conn, GUILD + 1, q2)["active"] == 1


def test_reactivating_same_quest_does_not_self_block(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="daily")
        set_quest_active(conn, GUILD, qid, True)  # already active, must not raise
        assert _get(conn, GUILD, qid)["active"] == 1


# ── instant claim ─────────────────────────────────────────────────────


def test_instant_claim_pays_and_records(db):
    with open_db(db) as conn:
        qid = _make(conn, reward=10)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)
        assert isinstance(out, ClaimOutcome)
        assert out.state == "paid"
        assert out.paid == 10
        assert get_balance(conn, GUILD, USER) == 10
        row = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE id = ?", (out.claim_id,)
        ).fetchone()
        assert row["state"] == "paid"


def test_instant_claim_booster_ceils(db):
    with open_db(db) as conn:
        qid = _make(conn, reward=5)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=True)
        assert out.paid == 8  # ceil(5 * 1.5)
        assert get_balance(conn, GUILD, USER) == 8


def test_instant_claim_twice_same_period_blocked(db):
    with open_db(db) as conn:
        qid = _make(conn)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)
        with pytest.raises(ValueError, match="already completed"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)
        assert get_balance(conn, GUILD, USER) == 10  # paid once only


def test_instant_claim_new_period_pays_again(db):
    with open_db(db) as conn:
        qid = _make(conn)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-11", booster=False)
        assert get_balance(conn, GUILD, USER) == 20


def test_paid_uniqueness_race_direct_duplicate_insert(db):
    # The partial unique index WHERE state='paid' is the money race anchor:
    # a second paid row for the same (quest,user,period) must be rejected at
    # the DB layer regardless of what the service does.
    with open_db(db) as conn:
        qid = _make(conn)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO econ_quest_claims
                    (quest_id, guild_id, user_id, period, state, created_at)
                VALUES (?, ?, ?, ?, 'paid', ?)
                """,
                (qid, GUILD, USER, "2026-07-10", time.time()),
            )


# ── claim guards ──────────────────────────────────────────────────────


def test_claim_missing_quest_raises(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="not found"):
            claim_quest(conn, SETTINGS, GUILD, 999, USER, period="2026-07-10", booster=False)


def test_claim_inactive_quest_raises(db):
    with open_db(db) as conn:
        qid = _make(conn, active=False)
        with pytest.raises(ValueError, match="not active"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)


def test_claim_wrong_guild_raises(db):
    with open_db(db) as conn:
        qid = _make(conn)
        with pytest.raises(ValueError, match="not found"):
            claim_quest(conn, SETTINGS, GUILD + 1, qid, USER, period="2026-07-10", booster=False)


def test_claim_before_start_raises(db):
    with open_db(db) as conn:
        qid = _make(conn, starts_at=time.time() + 3600)
        with pytest.raises(ValueError, match="not started"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)


def test_claim_after_end_raises(db):
    with open_db(db) as conn:
        qid = _make(conn, ends_at=time.time() - 3600)
        with pytest.raises(ValueError, match="ended"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)


def test_community_quest_not_directly_claimable(db):
    # Community quests pay via settlement; a self-claim would double-pay.
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=10)
        with pytest.raises(ValueError, match="cannot be claimed"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="once", booster=False)


# ── sign-off flow ─────────────────────────────────────────────────────


def test_signoff_claim_is_pending_no_credit(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        assert out.state == "pending"
        assert out.paid == 0
        assert get_balance(conn, GUILD, USER) == 0


def test_signoff_double_pending_blocked(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        with pytest.raises(ValueError, match="awaiting sign-off"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)


def test_pending_uniqueness_race_direct_duplicate_insert(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO econ_quest_claims
                    (quest_id, guild_id, user_id, period, state, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (qid, GUILD, USER, "2026-W28", time.time()),
            )


def test_signoff_approve_pays(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        res = resolve_claim(
            conn, SETTINGS, out.claim_id, approve=True, resolver_id=MANAGER, booster=False
        )
        assert res.paid == 50
        assert res.deny_reason is None
        assert res.user_id == USER
        assert get_balance(conn, GUILD, USER) == 50


def test_signoff_approve_booster_ceils(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        res = resolve_claim(
            conn, SETTINGS, out.claim_id, approve=True, resolver_id=MANAGER, booster=True
        )
        assert res.paid == 75  # ceil(50 * 1.5)


def test_deny_reclaim_approve_chain(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        first = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        res = resolve_claim(
            conn,
            SETTINGS,
            first.claim_id,
            approve=False,
            resolver_id=MANAGER,
            deny_reason="not enough proof",
            booster=False,
        )
        assert res.paid == 0
        assert res.deny_reason == "not enough proof"
        assert get_balance(conn, GUILD, USER) == 0
        # denied is re-claimable within the same period
        second = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        assert second.state == "pending"
        res2 = resolve_claim(
            conn, SETTINGS, second.claim_id, approve=True, resolver_id=MANAGER, booster=False
        )
        assert res2.paid == 50
        assert get_balance(conn, GUILD, USER) == 50


def test_resolve_non_pending_raises(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        resolve_claim(conn, SETTINGS, out.claim_id, approve=True, resolver_id=MANAGER, booster=False)
        with pytest.raises(ValueError, match="not pending"):
            resolve_claim(
                conn, SETTINGS, out.claim_id, approve=True, resolver_id=MANAGER, booster=False
            )


def test_resolve_missing_claim_raises(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="claim not found"):
            resolve_claim(conn, SETTINGS, 999, approve=True, resolver_id=MANAGER, booster=False)


def test_signoff_claim_blocked_after_paid_this_period(db):
    # Once a member is paid for the period, they cannot open a fresh pending.
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        resolve_claim(conn, SETTINGS, out.claim_id, approve=True, resolver_id=MANAGER, booster=False)
        with pytest.raises(ValueError, match="already completed"):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)


def test_approve_double_pay_backstop(db):
    # Money guard: if a paid row already exists for the period, approving a
    # stray pending must NOT credit again. We force the race by planting a
    # pending directly (bypassing the claim pre-check) after a paid exists.
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        first = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        resolve_claim(conn, SETTINGS, first.claim_id, approve=True, resolver_id=MANAGER, booster=False)
        assert get_balance(conn, GUILD, USER) == 50
        cur = conn.execute(
            """
            INSERT INTO econ_quest_claims
                (quest_id, guild_id, user_id, period, state, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (qid, GUILD, USER, "2026-W28", time.time()),
        )
        stray_id = int(cur.lastrowid or 0)
        with pytest.raises(ValueError, match="already completed"):
            resolve_claim(conn, SETTINGS, stray_id, approve=True, resolver_id=MANAGER, booster=False)
        assert get_balance(conn, GUILD, USER) == 50  # no double credit


def test_set_claim_card_records_ids(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        set_claim_card(conn, out.claim_id, 777, 888)
        row = conn.execute(
            "SELECT card_channel_id, card_message_id FROM econ_quest_claims WHERE id = ?",
            (out.claim_id,),
        ).fetchone()
        assert row["card_channel_id"] == 777
        assert row["card_message_id"] == 888


# ── expiry ────────────────────────────────────────────────────────────


def test_expire_transitions_and_returns_once(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        conn.execute(
            "UPDATE econ_quest_claims SET created_at = ? WHERE id = ?",
            (time.time() - 8 * 86400, out.claim_id),
        )
        expired = expire_stale_claims(conn, time.time())
        assert len(expired) == 1
        assert expired[0]["state"] == "expired"
        assert expired[0]["user_id"] == USER
        # A replay returns nothing — the transition already happened.
        assert expire_stale_claims(conn, time.time()) == []


def test_expire_leaves_fresh_pending(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        assert expire_stale_claims(conn, time.time()) == []


def test_expired_is_reclaimable(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        conn.execute(
            "UPDATE econ_quest_claims SET created_at = ? WHERE id = ?",
            (time.time() - 8 * 86400, out.claim_id),
        )
        expire_stale_claims(conn, time.time())
        again = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        assert again.state == "pending"


# ── deny history ──────────────────────────────────────────────────────


def test_deny_history_ordering(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        # two denies + one expiry, each older than the next
        for i, reason in enumerate(["first", "second"]):
            out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
            conn.execute(
                "UPDATE econ_quest_claims SET created_at = ? WHERE id = ?",
                (time.time() - (10 - i), out.claim_id),
            )
            resolve_claim(
                conn,
                SETTINGS,
                out.claim_id,
                approve=False,
                resolver_id=MANAGER,
                deny_reason=reason,
                booster=False,
            )
            conn.execute(
                "UPDATE econ_quest_claims SET resolved_at = ? WHERE id = ?",
                (100.0 + i, out.claim_id),
            )
        hist = deny_history(conn, qid, USER)
        assert len(hist) == 2
        # newest resolved first
        assert hist[0]["deny_reason"] == "second"
        assert hist[1]["deny_reason"] == "first"


def test_deny_history_excludes_paid_and_pending(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", reward=50, signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        resolve_claim(conn, SETTINGS, out.claim_id, approve=True, resolver_id=MANAGER, booster=False)
        assert deny_history(conn, qid, USER) == []


# ── list_claims ───────────────────────────────────────────────────────


def test_list_claims_filters_by_state(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        claim_quest(conn, SETTINGS, GUILD, qid, OTHER, period="2026-W28", booster=False)
        pending = list_claims(conn, GUILD, state="pending")
        assert len(pending) == 2
        assert {r["user_id"] for r in pending} == {USER, OTHER}
        assert list_claims(conn, GUILD, state="paid") == []


def test_list_claims_filters_by_quest(db):
    with open_db(db) as conn:
        q1 = _make(conn, qtype="weekly", signoff=1)
        q2 = _make(conn, qtype="weekly", signoff=1)
        claim_quest(conn, SETTINGS, GUILD, q1, USER, period="2026-W28", booster=False)
        claim_quest(conn, SETTINGS, GUILD, q2, USER, period="2026-W28", booster=False)
        assert len(list_claims(conn, GUILD, quest_id=q1)) == 1


# ── community quests ──────────────────────────────────────────────────


def test_progress_crossing_sets_completed_once(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=100)
        assert set_community_progress(conn, qid, 50, target=100) is False
        assert set_community_progress(conn, qid, 100, target=100) is True  # crossing
        assert set_community_progress(conn, qid, 150, target=100) is False  # already done
        row = conn.execute(
            "SELECT current, completed_at FROM econ_community_progress WHERE quest_id = ?",
            (qid,),
        ).fetchone()
        assert row["current"] == 150
        assert row["completed_at"] is not None


def test_progress_completed_at_stable_when_current_drops(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=10)
        set_community_progress(conn, qid, 10, target=10)
        first = conn.execute(
            "SELECT completed_at FROM econ_community_progress WHERE quest_id = ?", (qid,)
        ).fetchone()["completed_at"]
        set_community_progress(conn, qid, 3, target=10)  # dropped back below target
        after = conn.execute(
            "SELECT completed_at FROM econ_community_progress WHERE quest_id = ?", (qid,)
        ).fetchone()["completed_at"]
        assert after == first  # never cleared


def test_progress_first_row_can_cross_immediately(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=5)
        assert set_community_progress(conn, qid, 5, target=5) is True


def test_settle_pays_each_member_once(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=1)
        set_community_progress(conn, qid, 1, target=1)
        n = settle_community_quest(conn, SETTINGS, GUILD, qid, {USER: False, OTHER: True})
        assert n == 2
        assert get_balance(conn, GUILD, USER) == 30
        assert get_balance(conn, GUILD, OTHER) == 45  # ceil(30 * 1.5)
        settled = conn.execute(
            "SELECT settled_at FROM econ_community_progress WHERE quest_id = ?", (qid,)
        ).fetchone()["settled_at"]
        assert settled is not None


def test_settle_is_idempotent_and_pays_missed_remainder(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=1)
        set_community_progress(conn, qid, 1, target=1)
        n1 = settle_community_quest(conn, SETTINGS, GUILD, qid, {USER: False})
        assert n1 == 1
        # a second sweep with an extra member pays only the new member
        n2 = settle_community_quest(conn, SETTINGS, GUILD, qid, {USER: False, OTHER: False})
        assert n2 == 1
        assert get_balance(conn, GUILD, USER) == 30  # not double-paid
        assert get_balance(conn, GUILD, OTHER) == 30


def test_settle_crash_replay_pays_only_missed(db):
    # Simulate a mid-sweep crash: USER's payout row + credit landed, then the
    # process died before settled_at. A replay must pay only OTHER.
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=1)
        set_community_progress(conn, qid, 1, target=1)
        conn.execute(
            "INSERT INTO econ_community_payouts (quest_id, user_id) VALUES (?, ?)",
            (qid, USER),
        )
        from bot_modules.services.economy_service import apply_credit

        apply_credit(conn, GUILD, USER, 30, "quest_community", multiplier=1.5)
        n = settle_community_quest(conn, SETTINGS, GUILD, qid, {USER: False, OTHER: False})
        assert n == 1  # only OTHER
        assert get_balance(conn, GUILD, USER) == 30
        assert get_balance(conn, GUILD, OTHER) == 30


def test_settle_zero_reward_still_reserves(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=0, community_target=1)
        set_community_progress(conn, qid, 1, target=1)
        n = settle_community_quest(conn, SETTINGS, GUILD, qid, {USER: False})
        assert n == 1  # reserved even though nothing credited
        assert get_balance(conn, GUILD, USER) == 0


def test_list_settleable_excludes_signoff_and_unsettled(db):
    with open_db(db) as conn:
        # completed, no signoff -> settleable
        a = _make(conn, qtype="community", reward=30, community_target=1, active=False)
        set_community_progress(conn, a, 1, target=1)
        # completed but signoff=1 -> gated out (dashboard-only)
        b = _make(
            conn, qtype="community", reward=30, signoff=1, community_target=1, active=False
        )
        set_community_progress(conn, b, 1, target=1)
        # not yet completed -> excluded
        c = _make(conn, qtype="community", reward=30, community_target=5, active=False)
        set_community_progress(conn, c, 2, target=5)
        ids = [row["id"] for row in list_settleable_community_quests(conn, GUILD)]
        assert ids == [a]


def test_list_settleable_excludes_already_settled(db):
    with open_db(db) as conn:
        a = _make(conn, qtype="community", reward=30, community_target=1, active=False)
        set_community_progress(conn, a, 1, target=1)
        settle_community_quest(conn, SETTINGS, GUILD, a, {USER: False})
        assert list_settleable_community_quests(conn, GUILD) == []


def test_active_member_ids_windows_by_days(db):
    now = time.time()
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO member_activity VALUES (?, ?, 1, 1, ?)", (GUILD, USER, now - 5 * 86400)
        )
        conn.execute(
            "INSERT INTO member_activity VALUES (?, ?, 1, 1, ?)", (GUILD, OTHER, now - 40 * 86400)
        )
        ids = active_member_ids(conn, GUILD, days=30)
        assert ids == [USER]


def test_active_member_ids_scoped_to_guild(db):
    now = time.time()
    with open_db(db) as conn:
        conn.execute("INSERT INTO member_activity VALUES (?, ?, 1, 1, ?)", (GUILD, USER, now))
        conn.execute("INSERT INTO member_activity VALUES (?, ?, 1, 1, ?)", (GUILD + 1, OTHER, now))
        assert active_member_ids(conn, GUILD) == [USER]


# ── delete refusal ────────────────────────────────────────────────────


def test_delete_refused_with_paid_claim(db):
    with open_db(db) as conn:
        qid = _make(conn)
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-07-10", booster=False)
        with pytest.raises(ValueError, match="paid claims"):
            delete_quest(conn, GUILD, qid)
        assert get_quest(conn, GUILD, qid) is not None


def test_delete_allowed_without_paid_claims(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="weekly", signoff=1)
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-W28", booster=False)
        resolve_claim(
            conn, SETTINGS, out.claim_id, approve=False, resolver_id=MANAGER,
            deny_reason="no", booster=False,
        )
        delete_quest(conn, GUILD, qid)
        assert get_quest(conn, GUILD, qid) is None
        # claims cascaded
        assert conn.execute(
            "SELECT COUNT(*) c FROM econ_quest_claims WHERE quest_id = ?", (qid,)
        ).fetchone()["c"] == 0


def test_delete_cascades_community_rows(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="community", reward=30, community_target=1)
        set_community_progress(conn, qid, 1, target=1)
        settle_community_quest(conn, SETTINGS, GUILD, qid, {USER: False})
        delete_quest(conn, GUILD, qid)
        assert conn.execute(
            "SELECT COUNT(*) c FROM econ_community_progress WHERE quest_id = ?", (qid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM econ_community_payouts WHERE quest_id = ?", (qid,)
        ).fetchone()["c"] == 0


# ── rotation ──────────────────────────────────────────────────────────


def test_rotate_cycles_the_tagged_pool(db):
    with open_db(db) as conn:
        a = _make(conn, qtype="daily", rotate_tag="pool", active=True)
        b = _make(conn, qtype="daily", rotate_tag="pool", active=False)
        assert rotate_pool(conn, GUILD, "daily") == b
        assert _get(conn, GUILD, a)["active"] == 0
        assert _get(conn, GUILD, b)["active"] == 1
        # cycle back to a
        assert rotate_pool(conn, GUILD, "daily") == a
        assert _get(conn, GUILD, a)["active"] == 1


def test_rotate_noop_when_pool_of_one(db):
    with open_db(db) as conn:
        _make(conn, qtype="daily", rotate_tag="solo", active=True)
        assert rotate_pool(conn, GUILD, "daily") is None


def test_rotate_noop_when_no_active_tagged(db):
    with open_db(db) as conn:
        _make(conn, qtype="daily", rotate_tag="", active=True)
        _make(conn, qtype="daily", rotate_tag="pool", active=False)
        assert rotate_pool(conn, GUILD, "daily") is None


def test_rotate_respects_slot_rule(db):
    # Rotating a daily keeps exactly one daily active.
    with open_db(db) as conn:
        _make(conn, qtype="daily", rotate_tag="pool", active=True)
        _make(conn, qtype="daily", rotate_tag="pool", active=False)
        rotate_pool(conn, GUILD, "daily")
        active = list_quests(conn, GUILD, active_only=True)
        assert len([q for q in active if q["qtype"] == "daily"]) == 1


def test_rotate_empty_type_is_none(db):
    with open_db(db) as conn:
        assert rotate_pool(conn, GUILD, "weekly") is None


# ── trigger-word quests (spec §4.4) ───────────────────────────────────


def test_create_quest_persists_trigger_fields(db):
    with open_db(db) as conn:
        qid = _make(conn, trigger_words="gm, good morning", trigger_channel_id=777)
        row = _get(conn, GUILD, qid)
        assert row["trigger_words"] == "gm, good morning"
        assert row["trigger_channel_id"] == 777


def test_create_quest_trigger_fields_default_empty(db):
    with open_db(db) as conn:
        row = _get(conn, GUILD, _make(conn))
        assert row["trigger_words"] == ""
        assert row["trigger_channel_id"] is None


def test_update_quest_patches_trigger_fields(db):
    with open_db(db) as conn:
        qid = _make(conn)
        update_quest(
            conn, GUILD, qid,
            {"trigger_words": "hello", "trigger_channel_id": 42},
        )
        row = _get(conn, GUILD, qid)
        assert row["trigger_words"] == "hello"
        assert row["trigger_channel_id"] == 42
        update_quest(conn, GUILD, qid, {"trigger_words": "", "trigger_channel_id": None})
        row = _get(conn, GUILD, qid)
        assert row["trigger_words"] == ""
        assert row["trigger_channel_id"] is None


def test_list_trigger_quests_filters(db):
    with open_db(db) as conn:
        watched = _make(conn, trigger_words="gm")
        _make(conn, qtype="weekly", trigger_words="")  # no phrases
        _make(conn, qtype="weekly", trigger_words="hi", active=False)  # inactive
        _make(  # community quests are never member-claimable
            conn, qtype="community", trigger_words="hi", community_target=5
        )
        rows = list_trigger_quests(conn, GUILD)
        assert [int(r["id"]) for r in rows] == [watched]


def test_list_trigger_quests_scoped_to_guild(db):
    with open_db(db) as conn:
        _make(conn, trigger_words="gm", guild_id=GUILD + 1)
        assert list_trigger_quests(conn, GUILD) == []


# ── trigger-kind quests (event + daily/weekly auto-claims) ────────────


def test_trigger_kind_validation_matrix(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            _make(conn, qtype="event")  # event needs a kind
        with pytest.raises(ValueError):
            _make(conn, qtype="event", trigger_kind="nope")  # unknown kind
        with pytest.raises(ValueError):
            _make(conn, qtype="community", trigger_kind="duel", community_target=5)
        with pytest.raises(ValueError):  # words and kind are exclusive
            _make(conn, qtype="daily", trigger_kind="duel", trigger_words="gm")
        # Daily/weekly may carry a kind ("do it once this period")…
        daily = _make(conn, qtype="daily", trigger_kind="party_game")
        assert _get(conn, GUILD, daily)["trigger_kind"] == "party_game"
        # …and event quests always need one.
        qid = _make(conn, qtype="event", trigger_kind="photo_reply")
        assert _get(conn, GUILD, qid)["trigger_kind"] == "photo_reply"


def test_update_validates_trigger_config_pairing(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="event", trigger_kind="photo_reply", active=False)
        # Retyping to daily keeps the kind (legal: daily auto-claim).
        update_quest(conn, GUILD, qid, {"qtype": "daily"})
        assert _get(conn, GUILD, qid)["qtype"] == "daily"
        # Adding words while a kind is set is rejected…
        with pytest.raises(ValueError):
            update_quest(conn, GUILD, qid, {"trigger_words": "gm"})
        # …and going back to event with the kind cleared is rejected too.
        with pytest.raises(ValueError):
            update_quest(conn, GUILD, qid, {"qtype": "event", "trigger_kind": ""})


def test_event_slot_limit_is_per_trigger_kind(db):
    with open_db(db) as conn:
        _make(conn, qtype="event", trigger_kind="photo_reply")
        second = _make(
            conn, qtype="event", trigger_kind="photo_reply", active=False
        )
        with pytest.raises(SlotLimitError):
            set_quest_active(conn, GUILD, second, True)
        # A different kind coexists, and event quests eat no daily/weekly slot.
        _make(conn, qtype="event", trigger_kind="duel")
        _make(conn, qtype="daily")
        _make(conn, qtype="weekly")


def test_list_kind_triggered_quests_filters(db):
    with open_db(db) as conn:
        assert list_kind_triggered_quests(conn, GUILD, "photo_reply") == []
        _make(conn, qtype="event", trigger_kind="photo_reply", active=False)
        assert list_kind_triggered_quests(conn, GUILD, "photo_reply") == []
        event = _make(conn, qtype="event", trigger_kind="photo_reply")
        daily = _make(conn, qtype="daily", trigger_kind="photo_reply")
        _make(conn, qtype="event", trigger_kind="duel")  # other kind
        rows = list_kind_triggered_quests(conn, GUILD, "photo_reply")
        assert sorted(int(r["id"]) for r in rows) == sorted([event, daily])
        # Other guilds don't leak.
        assert list_kind_triggered_quests(conn, GUILD + 1, "photo_reply") == []


def test_event_claim_dedupes_per_occurrence_not_per_day(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="event", trigger_kind="photo_reply", reward=10)
        period = occurrence_period("photo_reply", "card-1")
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period=period, booster=False)
        assert out.state == "paid" and out.paid == 10
        # Same occurrence again → collision, no double pay.
        with pytest.raises(ValueError):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period=period, booster=False)
        # A different occurrence pays again; another member independently.
        claim_quest(
            conn, SETTINGS, GUILD, qid, USER,
            period=occurrence_period("photo_reply", "card-2"), booster=False,
        )
        claim_quest(conn, SETTINGS, GUILD, qid, OTHER, period=period, booster=False)
        assert get_balance(conn, GUILD, USER) == 20
        assert get_balance(conn, GUILD, OTHER) == 10


def test_fire_trigger_quests_daily_vs_event_cadence(db):
    with open_db(db) as conn:
        daily = _make(conn, qtype="daily", trigger_kind="duel", reward=10)
        event = _make(conn, qtype="event", trigger_kind="duel", reward=5)

        first = fire_trigger_quests(
            conn, SETTINGS, GUILD, "duel", USER,
            local_day="2026-07-12", occurrence="quickdraw:1", booster=False,
        )
        assert sorted(int(q["id"]) for q, _ in first) == sorted([daily, event])
        assert get_balance(conn, GUILD, USER) == 15

        # Second duel the same day: the daily is already claimed, the event
        # quest pays for the new occurrence.
        second = fire_trigger_quests(
            conn, SETTINGS, GUILD, "duel", USER,
            local_day="2026-07-12", occurrence="chicken:1", booster=False,
        )
        assert [int(q["id"]) for q, _ in second] == [event]
        assert get_balance(conn, GUILD, USER) == 20

        # Replaying the same occurrence pays nothing at all.
        assert fire_trigger_quests(
            conn, SETTINGS, GUILD, "duel", USER,
            local_day="2026-07-12", occurrence="chicken:1", booster=False,
        ) == []

        # Next local day: the daily fires again.
        third = fire_trigger_quests(
            conn, SETTINGS, GUILD, "duel", USER,
            local_day="2026-07-13", occurrence="quickdraw:2", booster=False,
        )
        assert sorted(int(q["id"]) for q, _ in third) == sorted([daily, event])


def test_fire_trigger_quests_without_occurrence_skips_event(db):
    with open_db(db) as conn:
        daily = _make(conn, qtype="daily", trigger_kind="party_game", reward=10)
        _make(conn, qtype="event", trigger_kind="party_game", reward=5)
        fired = fire_trigger_quests(
            conn, SETTINGS, GUILD, "party_game", USER,
            local_day="2026-07-12", occurrence=None, booster=False,
        )
        assert [int(q["id"]) for q, _ in fired] == [daily]
        assert get_balance(conn, GUILD, USER) == 10


# ── income sources (enable switches) ──────────────────────────────────


def test_income_sources_default_on_and_toggle(db):
    with open_db(db) as conn:
        states = list_income_sources(conn, GUILD)
        assert states and all(states.values())  # every kind, default enabled
        set_income_source(conn, GUILD, "duel", False)
        states = list_income_sources(conn, GUILD)
        assert states["duel"] is False and states["party_game"] is True
        set_income_source(conn, GUILD, "duel", True)
        assert source_enabled(conn, GUILD, "duel") is True
        with pytest.raises(ValueError):
            set_income_source(conn, GUILD, "nope", True)


def test_disabled_source_stops_firing(db):
    with open_db(db) as conn:
        _make(conn, qtype="daily", trigger_kind="duel", reward=10)
        set_income_source(conn, GUILD, "duel", False)
        assert fire_trigger_quests(
            conn, SETTINGS, GUILD, "duel", USER,
            local_day="2026-07-13", occurrence="quickdraw:1", booster=False,
        ) == []
        assert get_balance(conn, GUILD, USER) == 0
        # Re-enabling picks up where it left off — no state was consumed.
        set_income_source(conn, GUILD, "duel", True)
        fired = fire_trigger_quests(
            conn, SETTINGS, GUILD, "duel", USER,
            local_day="2026-07-13", occurrence="quickdraw:1", booster=False,
        )
        assert len(fired) == 1
        assert get_balance(conn, GUILD, USER) == 10


def test_fire_respects_channel_scope(db):
    with open_db(db) as conn:
        scoped = _make(
            conn, qtype="daily", trigger_kind="media_post", reward=10,
            trigger_channel_id=222,
        )
        # Wrong channel → nothing; matching channel (or thread parent) → pays.
        assert fire_trigger_quests(
            conn, SETTINGS, GUILD, "media_post", USER,
            local_day="2026-07-13", occurrence="m1", booster=False,
            channel_ids=(111,),
        ) == []
        # A channel-scoped quest never fires from a caller with no channel
        # context at all.
        assert fire_trigger_quests(
            conn, SETTINGS, GUILD, "media_post", USER,
            local_day="2026-07-13", occurrence="m2", booster=False,
        ) == []
        fired = fire_trigger_quests(
            conn, SETTINGS, GUILD, "media_post", USER,
            local_day="2026-07-13", occurrence="m3", booster=False,
            channel_ids=(333, 222),  # thread + parent
        )
        assert [int(q["id"]) for q, _ in fired] == [scoped]


def test_fire_trigger_inline_loads_settings_and_pays(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="event", trigger_kind="starboard", reward=10)
        # Economy disabled → no-op.
        assert fire_trigger_inline(
            conn, GUILD, "starboard", USER, occurrence="msg-1"
        ) == []
        save_econ_settings(conn, GUILD, {"enabled": True})
        fired = fire_trigger_inline(
            conn, GUILD, "starboard", USER, occurrence="msg-1"
        )
        assert [int(q["id"]) for q, _ in fired] == [qid]
        assert get_balance(conn, GUILD, USER) == 10
        # Same occurrence again → silent.
        assert fire_trigger_inline(
            conn, GUILD, "starboard", USER, occurrence="msg-1"
        ) == []


def test_photo_card_registry_roundtrip(db):
    with open_db(db) as conn:
        record_photo_card(conn, GUILD, 111, 9100, "game-1", "prompt")
        # Duplicate posts are ignored, not an error (INSERT OR IGNORE).
        record_photo_card(conn, GUILD, 111, 9100, "game-other", "prompt")
        row = get_photo_card(conn, GUILD, 9100)
        assert row is not None and row["game_id"] == "game-1"
        assert get_photo_card(conn, GUILD, 4242) is None
        # Guild-scoped: another guild can't claim against our card.
        assert get_photo_card(conn, GUILD + 1, 9100) is None
