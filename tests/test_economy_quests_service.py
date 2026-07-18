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
from bot_modules.economy.quests import (
    POOL_CAP,
    TRIGGER_KIND_INFO,
    TRIGGER_KINDS,
    effective_target,
    occurrence_period,
    quest_period,
)
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
    assigned_board_ids,
    expire_stale_claims,
    fire_trigger_inline,
    fire_trigger_quests,
    get_progress,
    get_quest,
    list_income_sources,
    list_kind_triggered_quests,
    list_quests,
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
    target_count=1,
    target_min=0,
    target_max=0,
    reward_xp=0,
    title="Quest",
):
    qid = create_quest(
        conn,
        guild_id,
        title=title,
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
        target_count=target_count,
        target_min=target_min,
        target_max=target_max,
        reward_xp=reward_xp,
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
                qtype="yearly",
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


def test_many_dailies_active_under_pool_cap(db):
    # The per-user board draws from the pool, so a cadence holds many active
    # quests — the old "one active daily" rule is gone.
    with open_db(db) as conn:
        for _ in range(3):
            _make(conn, qtype="daily")
        extra = _make(conn, qtype="daily", active=False)
        set_quest_active(conn, GUILD, extra, True)  # no raise
        assert _get(conn, GUILD, extra)["active"] == 1


def test_pool_cap_still_bounds_a_cadence(db):
    with open_db(db) as conn:
        for _ in range(POOL_CAP):
            _make(conn, qtype="weekly")
        over = _make(conn, qtype="weekly", active=False)
        with pytest.raises(SlotLimitError):
            set_quest_active(conn, GUILD, over, True)


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
        qid = _make(conn, qtype="event", trigger_kind="photo_react")
        assert _get(conn, GUILD, qid)["trigger_kind"] == "photo_react"


def test_update_validates_trigger_config_pairing(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="event", trigger_kind="photo_react", active=False)
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
        _make(conn, qtype="event", trigger_kind="photo_react")
        second = _make(
            conn, qtype="event", trigger_kind="photo_react", active=False
        )
        with pytest.raises(SlotLimitError):
            set_quest_active(conn, GUILD, second, True)
        # A different kind coexists, and event quests eat no daily/weekly slot.
        _make(conn, qtype="event", trigger_kind="duel")
        _make(conn, qtype="daily")
        _make(conn, qtype="weekly")


def test_list_kind_triggered_quests_filters(db):
    with open_db(db) as conn:
        assert list_kind_triggered_quests(conn, GUILD, "photo_react") == []
        _make(conn, qtype="event", trigger_kind="photo_react", active=False)
        assert list_kind_triggered_quests(conn, GUILD, "photo_react") == []
        event = _make(conn, qtype="event", trigger_kind="photo_react")
        daily = _make(conn, qtype="daily", trigger_kind="photo_react")
        _make(conn, qtype="event", trigger_kind="duel")  # other kind
        rows = list_kind_triggered_quests(conn, GUILD, "photo_react")
        assert sorted(int(r["id"]) for r in rows) == sorted([event, daily])
        # Other guilds don't leak.
        assert list_kind_triggered_quests(conn, GUILD + 1, "photo_react") == []


def test_event_claim_dedupes_per_occurrence_not_per_day(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="event", trigger_kind="photo_react", reward=10)
        period = occurrence_period("photo_react", "card-1")
        out = claim_quest(conn, SETTINGS, GUILD, qid, USER, period=period, booster=False)
        assert out.state == "paid" and out.paid == 10
        # Same occurrence again → collision, no double pay.
        with pytest.raises(ValueError):
            claim_quest(conn, SETTINGS, GUILD, qid, USER, period=period, booster=False)
        # A different occurrence pays again; another member independently.
        claim_quest(
            conn, SETTINGS, GUILD, qid, USER,
            period=occurrence_period("photo_react", "card-2"), booster=False,
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


def test_confession_quest_rejects_signoff(db):
    # A sign-off confession quest would post a bank-channel card naming the
    # confessor — the deanonymization the silent auto-claim exists to avoid.
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="deanonymize"):
            _make(conn, qtype="daily", trigger_kind="confession", signoff=1)
        # Non-sign-off confession quests are fine, and other kinds still allow
        # sign-off.
        _make(conn, qtype="daily", trigger_kind="confession", signoff=0)
        _make(conn, qtype="daily", trigger_kind="whisper", signoff=1)


def test_new_engagement_kinds_registered(db):
    # The confession/AMA/whisper/quote faucets must be full trigger kinds:
    # in TRIGGER_KINDS (dropdown + validation) with matching Income-Sources
    # copy, or their fire sites are dead code.
    for kind in ("confession", "ama_ask", "whisper", "quote"):
        assert kind in TRIGGER_KINDS, kind
        assert kind in TRIGGER_KIND_INFO, kind
        assert list_income_sources_has(db, kind)


def list_income_sources_has(db, kind):
    with open_db(db) as conn:
        return kind in list_income_sources(conn, GUILD)


@pytest.mark.parametrize("kind", ["confession", "ama_ask", "whisper", "quote"])
def test_new_engagement_kinds_fire_and_pay(db, kind):
    # A quest on each new kind pays once per occurrence, like any other event
    # quest — this is what the cog fire sites drive.
    with open_db(db) as conn:
        daily = _make(conn, qtype="daily", trigger_kind=kind, reward=7)
        event = _make(conn, qtype="event", trigger_kind=kind, reward=3)

        first = fire_trigger_quests(
            conn, SETTINGS, GUILD, kind, USER,
            local_day="2026-07-14", occurrence="a", booster=False,
        )
        assert sorted(int(q["id"]) for q, _ in first) == sorted([daily, event])
        assert get_balance(conn, GUILD, USER) == 10

        # Same occurrence replayed pays nothing; a new occurrence re-pays the
        # event quest (daily already claimed today).
        assert fire_trigger_quests(
            conn, SETTINGS, GUILD, kind, USER,
            local_day="2026-07-14", occurrence="a", booster=False,
        ) == []
        second = fire_trigger_quests(
            conn, SETTINGS, GUILD, kind, USER,
            local_day="2026-07-14", occurrence="b", booster=False,
        )
        assert [int(q["id"]) for q, _ in second] == [event]
        assert get_balance(conn, GUILD, USER) == 13


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


def test_target_count_validation(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):  # needs a trigger to count
            _make(conn, qtype="weekly", target_count=5)
        with pytest.raises(ValueError):  # events pay every occurrence
            _make(conn, qtype="event", trigger_kind="duel", target_count=5)
        with pytest.raises(ValueError):
            _make(conn, qtype="daily", trigger_kind="duel", target_count=0)
        qid = _make(conn, qtype="weekly", trigger_kind="reaction_given", target_count=5)
        assert _get(conn, GUILD, qid)["target_count"] == 5
        # Patching the trigger away while a target remains is rejected.
        with pytest.raises(ValueError):
            update_quest(conn, GUILD, qid, {"trigger_kind": ""})


def test_counted_quest_pays_at_target_with_occurrence_dedup(db):
    with open_db(db) as conn:
        qid = _make(
            conn, qtype="weekly", trigger_kind="reply_sent",
            reward=30, target_count=3,
        )

        def fire(occ, day="2026-07-13"):
            return fire_trigger_quests(
                conn, SETTINGS, GUILD, "reply_sent", USER,
                local_day=day, occurrence=occ, booster=False,
            )

        assert fire("m1") == []            # 1/3
        assert fire("m1") == []            # replayed occurrence: still 1/3
        assert fire("m2") == []            # 2/3
        assert get_progress(conn, qid, USER, "2026-W29") == 2
        fired = fire("m3")                 # 3/3 → pays
        assert len(fired) == 1 and fired[0][1].paid == 30
        assert get_balance(conn, GUILD, USER) == 30
        assert fire("m4") == []            # past target: no double pay
        # Next ISO week: a fresh count from zero.
        assert fire("n1", day="2026-07-20") == []
        assert get_progress(conn, qid, USER, "2026-W30") == 1


def test_counted_quest_needs_occurrence(db):
    with open_db(db) as conn:
        _make(conn, qtype="daily", trigger_kind="party_game", reward=10, target_count=2)
        assert fire_trigger_quests(
            conn, SETTINGS, GUILD, "party_game", USER,
            local_day="2026-07-13", occurrence=None, booster=False,
        ) == []
        assert get_balance(conn, GUILD, USER) == 0


def test_monthly_quest_claims_once_per_month(db):
    with open_db(db) as conn:
        qid = _make(conn, qtype="monthly", reward=100)
        out = claim_quest(
            conn, SETTINGS, GUILD, qid, USER, period="2026-07", booster=False
        )
        assert out.state == "paid" and out.paid == 100
        with pytest.raises(ValueError):
            claim_quest(
                conn, SETTINGS, GUILD, qid, USER, period="2026-07", booster=False
            )
        claim_quest(conn, SETTINGS, GUILD, qid, USER, period="2026-08", booster=False)
        assert get_balance(conn, GUILD, USER) == 200


def test_quest_xp_reward_pays_alongside_coins(db):
    with open_db(db) as conn:
        qid = _make(conn, reward=10, reward_xp=50)
        out = claim_quest(
            conn, SETTINGS, GUILD, qid, USER, period="2026-07-13", booster=True
        )
        assert out.paid == 15  # coins take the booster multiplier
        rows = conn.execute(
            "SELECT amount FROM xp_events WHERE guild_id = ? AND user_id = ? "
            "AND source = 'quest'",
            (GUILD, USER),
        ).fetchall()
        # XP is flat — no booster multiplier on the level curve.
        assert [int(r["amount"]) for r in rows] == [50]
        with pytest.raises(ValueError):
            _make(conn, reward_xp=-5)


def test_quest_level_up_is_announced_on_next_ordinary_award(db):
    """A level won by quest XP is still owed an announcement afterwards.

    Quest payouts credit XP from a sync DB context with no Discord handle, so
    nothing can announce at the time. The award path must not treat that level
    as already announced, or the level-up is lost for good: the next ordinary
    award derives its own start level from the already-credited total and so
    sees no change of its own.
    """
    from bot_modules.core.xp_system import apply_xp_award, level_for_xp

    with open_db(db) as conn:
        # 50 XP clears level 2 (15.6) but not level 3 (62.4).
        qid = _make(conn, reward=10, reward_xp=50)
        claim_quest(
            conn, SETTINGS, GUILD, qid, USER, period="2026-07-13", booster=False
        )

        row = conn.execute(
            "SELECT total_xp, level, announced_level FROM member_xp "
            "WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()
        assert level_for_xp(row["total_xp"]) == 2, "quest XP should clear level 2"
        assert row["level"] == 2
        # The quest had no way to announce, so the member has not been told.
        assert row["announced_level"] == 1

        # Next ordinary award: no level change of its own, but the level 2 the
        # quest won is still pending and must surface here.
        award = apply_xp_award(
            conn, GUILD, USER, 0.5, event_source="text", event_timestamp=time.time()
        )
        assert award.old_level == 2
        assert award.new_level == 2
        assert award.announced_level == 1
        assert award.new_level > award.announced_level, (
            "level 2 was won by quest XP and never announced -- "
            "handle_level_progress must still announce it"
        )


def test_quest_xp_paid_on_signoff_approval_not_filing(db):
    with open_db(db) as conn:
        qid = _make(conn, reward=10, reward_xp=25, signoff=1)
        out = claim_quest(
            conn, SETTINGS, GUILD, qid, USER, period="2026-07-13", booster=False
        )
        assert out.state == "pending"
        assert conn.execute(
            "SELECT COUNT(*) c FROM xp_events WHERE source = 'quest'"
        ).fetchone()["c"] == 0
        resolve_claim(
            conn, SETTINGS, out.claim_id, approve=True,
            resolver_id=MANAGER, booster=False,
        )
        assert conn.execute(
            "SELECT COUNT(*) c FROM xp_events WHERE source = 'quest'"
        ).fetchone()["c"] == 1


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


# ── per-user board + gaussian target (spec §4.6) ──────────────────────


def test_fire_only_pays_quests_on_members_board(db):
    # A pool of 6 same-kind dailies; a member earns only the 2 on their board.
    with open_db(db) as conn:
        for _ in range(6):
            _make(conn, qtype="daily", trigger_kind="message_sent")
        day = "2026-07-13"
        board = assigned_board_ids(conn, GUILD, USER, "daily", day)
        assert len(board) == 2  # PERSONAL_BOARD_SIZE["daily"]
        results = fire_trigger_quests(
            conn, SETTINGS, GUILD, "message_sent", USER,
            local_day=day, occurrence="m1", booster=False,
        )
        claimed = {int(q["id"]) for q, _ in results}
        assert claimed == board


def test_board_size_is_per_guild_configurable(db):
    # The guild's quest_board_* settings size the board, not the default 2.
    with open_db(db) as conn:
        for _ in range(6):
            _make(conn, qtype="daily", trigger_kind="message_sent")
        day = "2026-07-13"
        wide = EconSettings(enabled=True, quest_board_daily=4)
        narrow = EconSettings(enabled=True, quest_board_daily=1)
        assert len(assigned_board_ids(conn, GUILD, USER, "daily", day, wide)) == 4
        assert len(assigned_board_ids(conn, GUILD, USER, "daily", day, narrow)) == 1


def test_board_size_zero_pays_nothing(db):
    # 0 = cadence off: an empty board, and the trigger pays nothing. The
    # regression guarded here is the inverse — treating 0 as "no board" would
    # skip the filter and pay the whole pool.
    with open_db(db) as conn:
        for _ in range(6):
            _make(conn, qtype="daily", trigger_kind="message_sent")
        day = "2026-07-13"
        off = EconSettings(enabled=True, booster_multiplier=1.5, quest_board_daily=0)
        assert assigned_board_ids(conn, GUILD, USER, "daily", day, off) == set()
        results = fire_trigger_quests(
            conn, off, GUILD, "message_sent", USER,
            local_day=day, occurrence="m1", booster=False,
        )
        assert results == []


def test_board_size_zero_is_per_cadence(db):
    # Turning dailies off leaves the other cadences alone.
    with open_db(db) as conn:
        for _ in range(4):
            _make(conn, qtype="daily", trigger_kind="message_sent")
        for _ in range(4):
            _make(conn, qtype="weekly", trigger_kind="message_sent")
        day = "2026-07-13"
        cfg = EconSettings(
            enabled=True, booster_multiplier=1.5,
            quest_board_daily=0, quest_board_weekly=2,
        )
        assert assigned_board_ids(conn, GUILD, USER, "daily", day, cfg) == set()
        weekly = assigned_board_ids(conn, GUILD, USER, "weekly", day, cfg)
        assert len(weekly) == 2
        results = fire_trigger_quests(
            conn, cfg, GUILD, "message_sent", USER,
            local_day=day, occurrence="m1", booster=False,
        )
        # Only the weekly board paid — no daily leaked through.
        assert {int(q["id"]) for q, _ in results} == weekly


def test_board_differs_between_members(db):
    with open_db(db) as conn:
        for _ in range(6):
            _make(conn, qtype="daily", trigger_kind="message_sent")
        day = "2026-07-13"
        a = assigned_board_ids(conn, GUILD, USER, "daily", day)
        b = assigned_board_ids(conn, GUILD, OTHER, "daily", day)
        assert a != b  # different members draw different pairs


def test_gaussian_band_drives_counted_claim(db):
    # A banded counted quest fires only once its per-member target is reached.
    with open_db(db) as conn:
        qid = _make(
            conn, qtype="weekly", trigger_kind="message_sent",
            target_min=3, target_max=9,
        )
        day = "2026-07-13"
        period = quest_period("weekly", day)
        target = effective_target(1, 3, 9, user_id=USER, quest_id=qid, period=period)
        assert 3 <= target <= 9
        for i in range(target - 1):
            res = fire_trigger_quests(
                conn, SETTINGS, GUILD, "message_sent", USER,
                local_day=day, occurrence=f"m{i}", booster=False,
            )
            assert res == []  # target not reached yet — no claim
        res = fire_trigger_quests(
            conn, SETTINGS, GUILD, "message_sent", USER,
            local_day=day, occurrence=f"m{target - 1}", booster=False,
        )
        assert len(res) == 1  # crossing the drawn target pays


def test_create_quest_rejects_bad_band(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            _make(conn, qtype="weekly", trigger_kind="message_sent",
                  target_min=9, target_max=3)  # min !< max
