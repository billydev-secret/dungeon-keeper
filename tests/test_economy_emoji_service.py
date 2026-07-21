"""Tests for services/economy_emoji_service.py — paid, mod-approved emojis.

The money-critical paths: charged-at-submit, refund on every non-live exit
(deny, cancel, expiry, failed upload), refund exactly-once under replay, the
one-in-flight and unique-name claims, the two-phase approve (claim before the
Discord upload), and the graduation into a real econ_rentals row that bills
the animated rate from its meta.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.rentals import WEEK_SECONDS
from bot_modules.services.economy_emoji_service import (
    cancel_submission,
    claim_approval,
    deny_submission,
    emoji_price,
    expire_stale_submissions,
    fail_upload,
    finalize_upload,
    get_submission,
    mark_lapsed,
    open_submission,
    open_submission_count,
    sponsoring_enabled,
    submit_sponsorship,
    validate_emoji_name,
)
from bot_modules.services.economy_rentals_service import bill_rental
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from migrations import apply_migrations_sync

GUILD = 700
USER = 2001
USER_2 = 2002
MOD = 9001
NOW = 1_800_000_000.0

SETTINGS = EconSettings(
    enabled=True, price_emoji=60, price_emoji_animated=90,
    emoji_sponsor_slots=5, emoji_sponsor_expire_days=14,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant", actor_id=MOD)


def _submit(conn, *, user_id=USER, name="party_blob", animated=False, free=True):
    return submit_sponsorship(
        conn, SETTINGS, GUILD, user_id,
        name=name, image_path=f"/tmp/{name}.png", animated=animated,
        blocklist_patterns=[], taken_names=set(), guild_slots_free=free,
    )


def _ledger_kinds(conn, user_id=USER):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? "
            "AND user_id = ? ORDER BY id",
            (GUILD, user_id),
        )
    ]


# ── name validation ────────────────────────────────────────────────────


def test_validate_name_rules():
    assert validate_emoji_name(
        ":party_blob:", blocklist_patterns=[], taken_names=set()
    ) == "party_blob"
    for bad in ("x", "has space", "dash-y", "a" * 33, "émoji"):
        with pytest.raises(ValueError, match="2–32"):
            validate_emoji_name(bad, blocklist_patterns=[], taken_names=set())
    with pytest.raises(ValueError, match="isn't allowed"):
        validate_emoji_name(
            "badword_blob", blocklist_patterns=["badword"], taken_names=set()
        )
    # Collision is case-insensitive, matching Discord's :name: resolution.
    with pytest.raises(ValueError, match="already taken"):
        validate_emoji_name(
            "Party_Blob", blocklist_patterns=[], taken_names={"party_blob"}
        )


# ── submit ─────────────────────────────────────────────────────────────


def test_submit_charges_escrow_and_queues(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        assert out.price == 60
        assert get_balance(conn, GUILD, USER) == 40
        row = get_submission(conn, out.submission_id)
        assert row is not None and row["state"] == "pending"
        assert _ledger_kinds(conn)[-1] == ("emoji_sponsor", -60)


def test_submit_animated_bills_animated_price(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn, animated=True)
        assert out.price == 90
        assert emoji_price(SETTINGS, animated=True) == 90


def test_submit_disabled_and_insufficient_cost_nothing(db):
    off = EconSettings(enabled=True, price_emoji=0)
    assert sponsoring_enabled(off) is False
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="isn't enabled"):
            submit_sponsorship(
                conn, off, GUILD, USER, name="party_blob",
                image_path="/tmp/x.png", animated=False,
                blocklist_patterns=[], taken_names=set(), guild_slots_free=True,
            )
        _fund(conn, 10)
        with pytest.raises(ValueError, match="you have 10"):
            _submit(conn)
        assert get_balance(conn, GUILD, USER) == 10
        assert open_submission(conn, GUILD, USER) is None


def test_submit_one_in_flight_and_no_free_slots(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        _submit(conn)
        with pytest.raises(ValueError, match="in flight"):
            _submit(conn, name="second_try")
        _fund(conn, 100, user_id=USER_2)
        with pytest.raises(ValueError, match="slots"):
            _submit(conn, user_id=USER_2, name="other_blob", free=False)


def test_submit_duplicate_name_closed_by_index(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, user_id=USER_2)
        _submit(conn)
        # Same name, different member, stale taken_names → the partial unique
        # index still refuses (and the debit rolls back with the caller).
        with pytest.raises(ValueError, match="already taken"):
            _submit(conn, user_id=USER_2)


# ── cancel / deny refunds ──────────────────────────────────────────────


def test_cancel_pending_refunds_once(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        row = cancel_submission(conn, out.submission_id, user_id=USER)
        assert row["state"] == "cancelled"
        assert get_balance(conn, GUILD, USER) == 100
        with pytest.raises(ValueError, match="still-pending"):
            cancel_submission(conn, out.submission_id, user_id=USER)
        assert get_balance(conn, GUILD, USER) == 100  # exactly one refund


def test_cancel_wrong_user_refused(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        with pytest.raises(ValueError, match="isn't yours"):
            cancel_submission(conn, out.submission_id, user_id=USER_2)


def test_deny_pending_refunds_with_reason(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        row = deny_submission(
            conn, out.submission_id, resolver_id=MOD, deny_reason="nope"
        )
        assert row["state"] == "denied" and row["deny_reason"] == "nope"
        assert get_balance(conn, GUILD, USER) == 100
        assert _ledger_kinds(conn)[-1] == ("emoji_sponsor_refund", 60)
        with pytest.raises(ValueError, match="already denied"):
            deny_submission(conn, out.submission_id, resolver_id=MOD)


# ── two-phase approval ─────────────────────────────────────────────────


def test_claim_then_finalize_opens_rental(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn, animated=True)
        claim_approval(conn, out.submission_id, resolver_id=MOD)
        # A second resolver loses the claim race.
        with pytest.raises(ValueError, match="already approved"):
            claim_approval(conn, out.submission_id, resolver_id=MOD + 1)

        row = finalize_upload(
            conn, SETTINGS, out.submission_id, emoji_id=424242, now=NOW
        )
        assert row["state"] == "live" and row["emoji_id"] == 424242
        rental = conn.execute(
            "SELECT * FROM econ_rentals WHERE id = ?", (row["rental_id"],)
        ).fetchone()
        assert rental["perk"] == "emoji" and rental["state"] == "active"
        # Escrow covered week one — the first bill lands a week out.
        assert rental["next_bill_at"] == NOW + WEEK_SECONDS
        meta = json.loads(rental["meta"])
        assert meta["emoji_id"] == 424242 and meta["animated"] is True
        # No new charge at finalize; the submit escrow was the only debit.
        assert get_balance(conn, GUILD, USER) == 10


def test_failed_upload_denies_and_refunds(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        claim_approval(conn, out.submission_id, resolver_id=MOD)
        row = fail_upload(conn, out.submission_id, reason="no slots")
        assert row["state"] == "denied"
        assert "no slots" in row["deny_reason"]
        assert get_balance(conn, GUILD, USER) == 100


def test_finalize_requires_claim(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        with pytest.raises(ValueError, match="awaiting an upload"):
            finalize_upload(conn, SETTINGS, out.submission_id, emoji_id=1)


# ── billing renews at the meta'd rate ──────────────────────────────────


def test_live_rental_renews_at_animated_price(db):
    with open_db(db) as conn:
        _fund(conn, 300)
        out = _submit(conn, animated=True)
        claim_approval(conn, out.submission_id, resolver_id=MOD)
        row = finalize_upload(
            conn, SETTINGS, out.submission_id, emoji_id=7, now=NOW
        )
        rental = conn.execute(
            "SELECT * FROM econ_rentals WHERE id = ?", (row["rental_id"],)
        ).fetchone()
        before = get_balance(conn, GUILD, USER)
        result = bill_rental(conn, SETTINGS, rental, NOW + WEEK_SECONDS + 1)
        assert result.action == "charge"
        assert result.charged == 90  # price_emoji_animated, read from meta
        assert get_balance(conn, GUILD, USER) == before - 90


# ── lapse closes the submission ────────────────────────────────────────


def test_mark_lapsed_frees_slot_and_name(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = _submit(conn)
        claim_approval(conn, out.submission_id, resolver_id=MOD)
        row = finalize_upload(conn, SETTINGS, out.submission_id, emoji_id=9)
        assert open_submission_count(conn, GUILD) == 1

        closed = mark_lapsed(conn, int(row["rental_id"]))
        assert closed is not None and closed["emoji_id"] == 9
        assert open_submission_count(conn, GUILD) == 0
        # No refund — the member got the weeks they paid for.
        assert get_balance(conn, GUILD, USER) == 40
        # Slot and name both free again.
        _fund(conn, 100)
        _submit(conn)
        assert mark_lapsed(conn, int(row["rental_id"])) is None  # idempotent


# ── expiry sweep ───────────────────────────────────────────────────────


def test_expire_refunds_stale_pending_only(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, user_id=USER_2)
        stale = _submit(conn)
        fresh = _submit(conn, user_id=USER_2, name="fresh_blob")
        conn.execute(
            "UPDATE econ_emoji_submissions SET created_at = ? WHERE id = ?",
            (NOW - 15 * 86400, stale.submission_id),
        )
        conn.execute(
            "UPDATE econ_emoji_submissions SET created_at = ? WHERE id = ?",
            (NOW - 1 * 86400, fresh.submission_id),
        )
        expired = expire_stale_submissions(conn, NOW, expire_days=14)
        assert [int(r["id"]) for r in expired] == [stale.submission_id]
        assert get_balance(conn, GUILD, USER) == 100  # refunded
        assert get_balance(conn, GUILD, USER_2) == 40  # untouched
        # Sweep off at 0 days.
        assert expire_stale_submissions(conn, NOW, expire_days=0) == []
