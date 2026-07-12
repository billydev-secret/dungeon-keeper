"""Tests for services/economy_rentals_service.py — the rental billing DB layer.

Covers the money-critical paths: upfront charge + price snapshot, the live
duplicate race (asserted via the public API), owner/force/grace cancels,
member-remove cleanup on both owner and beneficiary sides, the full billing
matrix (renewal no-drift, multi-week single charge, grace entry, retry
recovery, 36h revoke, period-end cancel), the suspension clock freeze/resume,
beneficiary-based entitlements, and personal-role CRUD.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.rentals import GRACE_SECONDS, WEEK_SECONDS
from bot_modules.services.economy_rentals_service import (
    BillingResult,
    bill_rental,
    cancel_all_for_member,
    cancel_rental,
    delete_personal_role,
    entitlements,
    get_personal_role,
    list_member_rentals,
    list_rentals,
    rent_perk,
    set_rental_suspended,
    upsert_personal_role,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
    get_ledger,
)
from migrations import apply_migrations_sync

GUILD = 500
USER = 1001
OTHER = 1002
THIRD = 1003
T0 = 1_000_000.0

SETTINGS = EconSettings(enabled=True, booster_multiplier=1.5)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(db, user, amount, guild=GUILD):
    with open_db(db) as conn:
        apply_credit(conn, guild, user, amount, "grant")


def get_balance_helper(db, user):
    with open_db(db) as conn:
        return get_balance(conn, GUILD, user)


def _rent(db, user, perk, *, beneficiary_id=None, now=T0, settings=SETTINGS):
    with open_db(db) as conn:
        row = rent_perk(
            conn, settings, GUILD, user, perk,
            beneficiary_id=beneficiary_id, now=now,
        )
        return dict(row)


def _get(db, rental_id):
    with open_db(db) as conn:
        return dict(
            conn.execute(
                "SELECT * FROM econ_rentals WHERE id = ?", (rental_id,)
            ).fetchone()
        )


def _set(db, rental_id, **cols):
    assigns = ", ".join(f"{k} = ?" for k in cols)
    with open_db(db) as conn:
        conn.execute(
            f"UPDATE econ_rentals SET {assigns} WHERE id = ?",
            (*cols.values(), rental_id),
        )


# ── rent_perk ──────────────────────────────────────────────────────────


def test_rent_charges_upfront_and_snapshots_price(db):
    _fund(db, USER, 200)
    row = _rent(db, USER, "role_color")
    assert row["state"] == "active"
    assert row["price"] == SETTINGS.price_role_color  # 50 default
    assert row["beneficiary_id"] == USER
    assert row["next_bill_at"] == T0 + WEEK_SECONDS
    assert row["started_at"] == T0
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 200 - SETTINGS.price_role_color
        led = get_ledger(conn, GUILD, USER, limit=1)[0]
        assert led["kind"] == "rental"
        assert led["amount"] == -SETTINGS.price_role_color


def test_rent_price_snapshot_uses_current_settings(db):
    _fund(db, USER, 500)
    pricey = EconSettings(price_role_color=99)
    row = _rent(db, USER, "role_color", settings=pricey)
    assert row["price"] == 99
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 500 - 99


def test_rent_insufficient_is_zero_write(db):
    _fund(db, USER, 30)  # role_color costs 50
    # The ValueError must escape open_db so the rolled-back transaction unwinds
    # the speculative rental INSERT (mirrors the cog letting it propagate).
    with pytest.raises(ValueError, match="insufficient"):
        with open_db(db) as conn:
            rent_perk(conn, SETTINGS, GUILD, USER, "role_color", now=T0)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 30
        assert list_rentals(conn, GUILD) == []
        # Only the seed grant remains — no "rental" debit row.
        assert all(r["kind"] != "rental" for r in get_ledger(conn, GUILD, USER, limit=5))


def test_rent_unknown_perk_rejected_no_charge(db):
    _fund(db, USER, 200)
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="unknown perk"):
            rent_perk(conn, SETTINGS, GUILD, USER, "role_wings", now=T0)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 200


def test_rent_duplicate_live_rejected_and_no_double_charge(db):
    _fund(db, USER, 200)
    _rent(db, USER, "role_color")
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="already rented"):
            rent_perk(conn, SETTINGS, GUILD, USER, "role_color", now=T0 + 100)
    with open_db(db) as conn:
        # Charged exactly once, one live rental.
        assert get_balance(conn, GUILD, USER) == 200 - SETTINGS.price_role_color
        assert len(list_rentals(conn, GUILD)) == 1


def test_rent_gift_duplicate_same_beneficiary_rejected(db):
    _fund(db, USER, 300)
    _rent(db, USER, "gift_color", beneficiary_id=OTHER)
    with pytest.raises(ValueError, match="already rented"):
        with open_db(db) as conn:
            rent_perk(
                conn, SETTINGS, GUILD, USER, "gift_color",
                beneficiary_id=OTHER, now=T0 + 5,
            )


def test_rent_gift_different_beneficiaries_both_succeed(db):
    # Proves beneficiary_id participates in the live-rental unique index: the
    # same payer + perk with two different beneficiaries is NOT a collision.
    _fund(db, USER, 300)
    a = _rent(db, USER, "gift_color", beneficiary_id=OTHER)
    b = _rent(db, USER, "gift_color", beneficiary_id=THIRD, now=T0 + 5)
    assert a["id"] != b["id"]
    assert a["beneficiary_id"] == OTHER
    assert b["beneficiary_id"] == THIRD


def test_rent_same_perk_allowed_after_lapse(db):
    _fund(db, USER, 300)
    first = _rent(db, USER, "role_color")
    _set(db, first["id"], state="lapsed")
    # A new rental of the same perk is fine once the old one is terminal.
    second = _rent(db, USER, "role_color", now=T0 + 999)
    assert second["id"] != first["id"]
    assert second["state"] == "active"


def test_rent_gift_beneficiary_is_the_friend(db):
    _fund(db, USER, 200)
    row = _rent(db, USER, "gift_color", beneficiary_id=OTHER)
    assert row["user_id"] == USER  # payer
    assert row["beneficiary_id"] == OTHER  # friend


# ── cancel_rental ──────────────────────────────────────────────────────


def test_cancel_active_by_owner_runs_to_period_end(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    with open_db(db) as conn:
        out = cancel_rental(conn, GUILD, r["id"], requester_id=USER)
    assert out["state"] == "active"  # still active until anniversary
    assert out["cancel_at_period_end"] == 1


def test_cancel_active_by_force(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    with open_db(db) as conn:
        out = cancel_rental(conn, GUILD, r["id"], requester_id=9999, force=True)
    assert out["cancel_at_period_end"] == 1


def test_cancel_grace_is_immediate(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    _set(db, r["id"], state="grace", grace_since=T0)
    with open_db(db) as conn:
        out = cancel_rental(conn, GUILD, r["id"], requester_id=USER)
    assert out["state"] == "cancelled"


def test_cancel_not_owner_rejected(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="not your rental"):
            cancel_rental(conn, GUILD, r["id"], requester_id=OTHER)


def test_cancel_missing_rejected(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="not found"):
            cancel_rental(conn, GUILD, 424242, requester_id=USER)


def test_cancel_terminal_rejected(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    _set(db, r["id"], state="lapsed")
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="not live"):
            cancel_rental(conn, GUILD, r["id"], requester_id=USER)


# ── cancel_all_for_member ──────────────────────────────────────────────


def test_cancel_all_for_member_hits_owner_and_beneficiary(db):
    _fund(db, USER, 200)
    _fund(db, OTHER, 200)
    _fund(db, THIRD, 200)
    owned = _rent(db, USER, "role_color")  # USER owns
    gift = _rent(db, OTHER, "gift_color", beneficiary_id=USER)  # USER benefits
    unrelated = _rent(db, THIRD, "role_color")  # untouched
    with open_db(db) as conn:
        affected = cancel_all_for_member(conn, GUILD, USER, now=T0 + 1)
    ids = {r["id"] for r in affected}
    assert ids == {owned["id"], gift["id"]}
    assert all(r["state"] == "cancelled" for r in affected)
    assert _get(db, unrelated["id"])["state"] == "active"


def test_cancel_all_for_member_none_live_returns_empty(db):
    with open_db(db) as conn:
        assert cancel_all_for_member(conn, GUILD, USER, now=T0) == []


# ── bill_rental: the billing matrix ────────────────────────────────────


def _bill(db, rental_id, now):
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM econ_rentals WHERE id = ?", (rental_id,)
        ).fetchone()
        return bill_rental(conn, SETTINGS, row, now)


def test_bill_not_due_is_noop(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    res = _bill(db, r["id"], T0 + 10)  # next_bill is T0 + WEEK
    assert isinstance(res, BillingResult)
    assert res.action == "none"
    assert res.charged == 0
    assert _get(db, r["id"])["next_bill_at"] == T0 + WEEK_SECONDS


def test_bill_renewal_advances_from_scheduled_not_now(db):
    _fund(db, USER, 200)
    r = _rent(db, USER, "role_color")
    # Loop runs 100s late past the anniversary.
    late = T0 + WEEK_SECONDS + 100
    res = _bill(db, r["id"], late)
    assert res.action == "charge"
    assert res.charged == SETTINGS.price_role_color
    row = _get(db, r["id"])
    # No drift: exactly two weeks from T0, NOT now + WEEK.
    assert row["next_bill_at"] == T0 + 2 * WEEK_SECONDS
    assert row["state"] == "active"
    with open_db(db) as conn:
        # rent + one renewal debit.
        rows = [x for x in get_ledger(conn, GUILD, USER, limit=9) if x["kind"] == "rental"]
        assert len(rows) == 2


def test_bill_multiweek_downtime_charges_once(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_color")
    # Loop was down for ~5 weeks.
    now = T0 + 5 * WEEK_SECONDS + 50
    res = _bill(db, r["id"], now)
    assert res.action == "charge"
    row = _get(db, r["id"])
    # Charged ONCE; next anniversary jumps to the first future one.
    assert row["next_bill_at"] == T0 + 6 * WEEK_SECONDS
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 500 - 2 * SETTINGS.price_role_color
        renewals = [
            x for x in get_ledger(conn, GUILD, USER, limit=20) if x["kind"] == "rental"
        ]
        assert len(renewals) == 2  # upfront + one renewal


def test_bill_renewal_uses_current_price(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_color")  # snapshot 50
    dearer = EconSettings(price_role_color=80)
    with open_db(db) as conn:
        row = conn.execute("SELECT * FROM econ_rentals WHERE id = ?", (r["id"],)).fetchone()
        res = bill_rental(conn, dearer, row, T0 + WEEK_SECONDS + 1)
    assert res.charged == 80
    assert _get(db, r["id"])["price"] == 80  # snapshot refreshed to current


def test_bill_insufficient_enters_grace(db):
    _fund(db, USER, SETTINGS.price_role_color)  # exactly one week
    r = _rent(db, USER, "role_color")  # balance now 0
    res = _bill(db, r["id"], T0 + WEEK_SECONDS + 1)
    assert res.action == "enter_grace"
    assert res.charged == 0
    row = _get(db, r["id"])
    assert row["state"] == "grace"
    assert row["grace_since"] == T0 + WEEK_SECONDS + 1
    # next_bill unchanged while in grace.
    assert row["next_bill_at"] == T0 + WEEK_SECONDS


def test_bill_retry_still_failing_stays_grace_silent(db):
    _fund(db, USER, SETTINGS.price_role_color)
    r = _rent(db, USER, "role_color")
    _set(db, r["id"], state="grace", grace_since=T0 + WEEK_SECONDS)
    res = _bill(db, r["id"], T0 + WEEK_SECONDS + 3600)  # 1h into grace, still broke
    assert res.action == "retry"
    row = _get(db, r["id"])
    assert row["state"] == "grace"
    assert row["grace_since"] == T0 + WEEK_SECONDS  # anchor unchanged


def test_bill_retry_recovers_and_advances_from_scheduled(db):
    _fund(db, USER, SETTINGS.price_role_color)
    r = _rent(db, USER, "role_color")  # balance 0
    _set(db, r["id"], state="grace", grace_since=T0 + WEEK_SECONDS)
    _fund(db, USER, SETTINGS.price_role_color)  # top up
    res = _bill(db, r["id"], T0 + WEEK_SECONDS + 7200)  # 2h into grace
    assert res.action == "charge"
    assert res.charged == SETTINGS.price_role_color
    row = _get(db, r["id"])
    assert row["state"] == "active"
    assert row["grace_since"] is None
    # Advances from the ORIGINAL scheduled anniversary, not from now.
    assert row["next_bill_at"] == T0 + 2 * WEEK_SECONDS


def test_bill_grace_36h_revokes_without_charging(db):
    _fund(db, USER, 500)  # funded, but revoke must NOT charge
    r = _rent(db, USER, "role_color")
    grace_start = T0 + WEEK_SECONDS
    _set(db, r["id"], state="grace", grace_since=grace_start)
    before = get_balance_helper(db, USER)
    res = _bill(db, r["id"], grace_start + GRACE_SECONDS)  # exactly 36h → revoke
    assert res.action == "revoke"
    assert res.charged == 0
    assert _get(db, r["id"])["state"] == "lapsed"
    assert get_balance_helper(db, USER) == before  # no debit on revoke


def test_bill_cancel_at_period_end_finalizes_without_charge(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_color")
    _set(db, r["id"], cancel_at_period_end=1)
    before = get_balance_helper(db, USER)
    res = _bill(db, r["id"], T0 + WEEK_SECONDS + 1)
    assert res.action == "cancel_period_end"
    assert res.charged == 0
    assert _get(db, r["id"])["state"] == "cancelled"
    assert get_balance_helper(db, USER) == before


# ── suspension clock ───────────────────────────────────────────────────


def test_suspend_freezes_billing(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_icon")
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], True, now=T0 + 10)
    row = _get(db, r["id"])
    assert row["suspended"] == 1
    assert row["suspended_since"] == T0 + 10
    # Even past the anniversary, a suspended rental does not bill.
    res = _bill(db, r["id"], T0 + WEEK_SECONDS + 500)
    assert res.action == "none"
    assert _get(db, r["id"])["state"] == "active"


def test_resume_pushes_next_bill_by_suspension_span(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_icon")
    orig_next = _get(db, r["id"])["next_bill_at"]
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], True, now=T0 + 100)
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], False, now=T0 + 100 + 3600)  # 1h frozen
    row = _get(db, r["id"])
    assert row["suspended"] == 0
    assert row["suspended_since"] is None
    assert row["next_bill_at"] == orig_next + 3600


def test_resume_also_pushes_grace_since_when_set(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_icon")
    _set(db, r["id"], state="grace", grace_since=T0 + 50)
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], True, now=T0 + 60)
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], False, now=T0 + 60 + 200)
    assert _get(db, r["id"])["grace_since"] == T0 + 50 + 200


def test_suspend_is_idempotent(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_icon")
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], True, now=T0 + 10)
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], True, now=T0 + 99)  # no-op
    assert _get(db, r["id"])["suspended_since"] == T0 + 10  # first wins


def test_resume_when_not_suspended_is_noop(db):
    _fund(db, USER, 500)
    r = _rent(db, USER, "role_icon")
    orig_next = _get(db, r["id"])["next_bill_at"]
    with open_db(db) as conn:
        set_rental_suspended(conn, r["id"], False, now=T0 + 5000)
    assert _get(db, r["id"])["next_bill_at"] == orig_next


# ── entitlements ───────────────────────────────────────────────────────


def test_entitlements_owner_and_gift_beneficiary(db):
    _fund(db, USER, 500)
    _fund(db, OTHER, 500)
    _rent(db, USER, "role_color")  # USER owns + benefits
    _rent(db, OTHER, "gift_color", beneficiary_id=USER)  # USER benefits, OTHER pays
    with open_db(db) as conn:
        assert entitlements(conn, GUILD, USER) == {"role_color", "gift_color"}
        # OTHER paid but is not the beneficiary of the gift.
        assert entitlements(conn, GUILD, OTHER) == set()


def test_entitlements_grace_grants_lapsed_does_not(db):
    _fund(db, USER, 500)
    r1 = _rent(db, USER, "role_color")
    r2 = _rent(db, USER, "role_icon")
    _set(db, r1["id"], state="grace", grace_since=T0)
    _set(db, r2["id"], state="lapsed")
    with open_db(db) as conn:
        assert entitlements(conn, GUILD, USER) == {"role_color"}


# ── list helpers ───────────────────────────────────────────────────────


def test_list_rentals_default_live_only(db):
    _fund(db, USER, 500)
    live = _rent(db, USER, "role_color")
    dead = _rent(db, USER, "role_icon")
    _set(db, dead["id"], state="lapsed")
    with open_db(db) as conn:
        ids = {r["id"] for r in list_rentals(conn, GUILD)}
        assert ids == {live["id"]}
        all_ids = {r["id"] for r in list_rentals(conn, GUILD, states=("active", "lapsed"))}
        assert all_ids == {live["id"], dead["id"]}


def test_list_member_rentals_owner_or_beneficiary(db):
    _fund(db, USER, 500)
    _fund(db, OTHER, 500)
    owned = _rent(db, USER, "role_color")
    gift = _rent(db, OTHER, "gift_color", beneficiary_id=USER)
    with open_db(db) as conn:
        ids = {r["id"] for r in list_member_rentals(conn, GUILD, USER)}
    assert ids == {owned["id"], gift["id"]}


# ── personal-role CRUD ─────────────────────────────────────────────────


def test_personal_role_none_initially(db):
    with open_db(db) as conn:
        assert get_personal_role(conn, GUILD, USER) is None


def test_personal_role_upsert_partial_then_patch(db):
    with open_db(db) as conn:
        upsert_personal_role(conn, GUILD, USER, {"name": "Stardust"})
    with open_db(db) as conn:
        row = get_personal_role(conn, GUILD, USER)
        assert row is not None
        assert row["name"] == "Stardust"
        assert row["color"] == -1  # default preserved
    with open_db(db) as conn:
        upsert_personal_role(conn, GUILD, USER, {"color": 0xFF00FF})
    with open_db(db) as conn:
        row = get_personal_role(conn, GUILD, USER)
        assert row is not None
        assert row["name"] == "Stardust"  # preserved across patch
        assert row["color"] == 0xFF00FF


def test_personal_role_unknown_field_rejected(db):
    with open_db(db) as conn:
        with pytest.raises(KeyError):
            upsert_personal_role(conn, GUILD, USER, {"bogus": 1})


def test_personal_role_delete(db):
    with open_db(db) as conn:
        upsert_personal_role(conn, GUILD, USER, {"name": "x", "role_id": 42})
    with open_db(db) as conn:
        delete_personal_role(conn, GUILD, USER)
    with open_db(db) as conn:
        assert get_personal_role(conn, GUILD, USER) is None
