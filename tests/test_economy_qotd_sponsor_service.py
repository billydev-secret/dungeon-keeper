"""Tests for services/economy_qotd_sponsor_service.py — the paid QOTD queue.

The money-critical paths: charged-at-submit, refund on every non-posting exit
(deny, withdraw, expiry), refund exactly-once under replay, and the guards that
stop two mods double-resolving or double-claiming the same queued question.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_qotd_sponsor_service import (
    MAX_QUESTION_LEN,
    attach_qotd,
    claim_next_approved,
    expire_stale_submissions,
    get_submission,
    list_submissions,
    mark_posted,
    next_approved,
    open_submission,
    release_claim,
    resolve_submission,
    sponsor_enabled,
    submit_sponsor,
    withdraw_approved,
)
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
DAY = 86400.0

SETTINGS = EconSettings(enabled=True, price_qotd_sponsor=40, qotd_sponsor_expire_days=14)
QUESTION = "What is the strangest thing you have ever eaten?"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant", actor_id=MOD)


def _ledger_kinds(conn, user_id=USER):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id",
            (GUILD, user_id),
        )
    ]


# ── submit ─────────────────────────────────────────────────────────────


def test_submit_charges_and_queues(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        assert out.price == 40
        assert get_balance(conn, GUILD, USER) == 60
        row = get_submission(conn, out.submission_id)
        assert row is not None
        assert row["state"] == "pending"
        assert row["price"] == 40
        assert row["question"] == QUESTION
        assert ("qotd_sponsor", -40) in _ledger_kinds(conn)


def test_submit_normalises_whitespace(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_sponsor(
            conn, SETTINGS, GUILD, USER, "  What   is\n\nyour favourite  colour? "
        )
        row = get_submission(conn, out.submission_id)
        assert row is not None
        assert row["question"] == "What is your favourite colour?"


def test_submit_rejects_short_long_and_never_charges(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        with pytest.raises(ValueError, match="a bit short"):
            submit_sponsor(conn, SETTINGS, GUILD, USER, "hi?")
        with pytest.raises(ValueError, match="limited to"):
            submit_sponsor(conn, SETTINGS, GUILD, USER, "x" * (MAX_QUESTION_LEN + 1))
        assert get_balance(conn, GUILD, USER) == 100
        assert list_submissions(conn, GUILD) == []


def test_submit_refuses_a_second_open_question(db):
    with open_db(db) as conn:
        _fund(conn, 500)
        submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        with pytest.raises(ValueError, match="already have a question"):
            submit_sponsor(conn, SETTINGS, GUILD, USER, "A different question here?")
        assert get_balance(conn, GUILD, USER) == 460  # charged once, not twice

        # Approved still counts as in flight…
        row = open_submission(conn, GUILD, USER)
        assert row is not None
        resolve_submission(conn, int(row["id"]), approve=True, resolver_id=MOD)
        with pytest.raises(ValueError, match="already have a question"):
            submit_sponsor(conn, SETTINGS, GUILD, USER, "A different question here?")

        # …but once it's posted, the member can sponsor again.
        assert mark_posted(conn, int(row["id"]), 123)
        submit_sponsor(conn, SETTINGS, GUILD, USER, "A different question here?")
        assert get_balance(conn, GUILD, USER) == 420


def test_submit_short_on_funds_changes_nothing(db):
    with open_db(db) as conn:
        _fund(conn, 10)
        with pytest.raises(ValueError, match="you have 10"):
            submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        assert get_balance(conn, GUILD, USER) == 10
        assert list_submissions(conn, GUILD) == []


def test_submit_disabled_at_zero_price(db):
    from dataclasses import replace

    off = replace(SETTINGS, price_qotd_sponsor=0)
    assert not sponsor_enabled(off)
    with open_db(db) as conn:
        _fund(conn, 100)
        with pytest.raises(ValueError, match="isn't enabled"):
            submit_sponsor(conn, off, GUILD, USER, QUESTION)
        assert get_balance(conn, GUILD, USER) == 100


# ── approve / deny ─────────────────────────────────────────────────────


def test_approve_queues_without_refunding(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        row = resolve_submission(conn, sid, approve=True, resolver_id=MOD)
        assert row["state"] == "approved"
        assert row["resolver_id"] == MOD
        assert row["refunded_at"] is None
        assert get_balance(conn, GUILD, USER) == 60  # still spent
        queued = next_approved(conn, GUILD)
        assert queued is not None and int(queued["id"]) == sid


def test_deny_refunds_with_reason(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        row = resolve_submission(
            conn, sid, approve=False, resolver_id=MOD, deny_reason="Too spicy"
        )
        assert row["state"] == "denied"
        assert row["deny_reason"] == "Too spicy"
        assert get_balance(conn, GUILD, USER) == 100  # made whole
        assert ("qotd_sponsor_refund", 40) in _ledger_kinds(conn)
        assert next_approved(conn, GUILD) is None


def test_resolving_twice_is_refused_and_never_double_refunds(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        resolve_submission(conn, sid, approve=False, resolver_id=MOD)
        assert get_balance(conn, GUILD, USER) == 100
        with pytest.raises(ValueError, match="already denied"):
            resolve_submission(conn, sid, approve=False, resolver_id=MOD)
        with pytest.raises(ValueError, match="already denied"):
            resolve_submission(conn, sid, approve=True, resolver_id=MOD)
        # Exactly one refund credit, whatever the second call tried to do.
        refunds = [k for k in _ledger_kinds(conn) if k[0] == "qotd_sponsor_refund"]
        assert refunds == [("qotd_sponsor_refund", 40)]
        assert get_balance(conn, GUILD, USER) == 100


def test_resolve_missing_submission(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="no longer exists"):
            resolve_submission(conn, 4242, approve=True, resolver_id=MOD)


def test_approved_cannot_be_re_resolved_only_withdrawn(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        resolve_submission(conn, sid, approve=True, resolver_id=MOD)
        with pytest.raises(ValueError, match="already approved"):
            resolve_submission(conn, sid, approve=False, resolver_id=MOD)
        assert get_balance(conn, GUILD, USER) == 60  # no sneaky refund

        row = withdraw_approved(conn, sid, resolver_id=MOD, reason="changed our minds")
        assert row["state"] == "denied"
        assert get_balance(conn, GUILD, USER) == 100
        assert next_approved(conn, GUILD) is None


def test_withdraw_only_touches_approved(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        with pytest.raises(ValueError, match="isn't waiting"):
            withdraw_approved(conn, sid, resolver_id=MOD)
        assert get_balance(conn, GUILD, USER) == 60  # pending money stays spent


# ── posting ────────────────────────────────────────────────────────────


def test_queue_is_fifo_and_claim_is_exclusive(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, USER_2)
        first = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        second = submit_sponsor(
            conn, SETTINGS, GUILD, USER_2, "Second question goes here?"
        ).submission_id
        resolve_submission(conn, second, approve=True, resolver_id=MOD)
        resolve_submission(conn, first, approve=True, resolver_id=MOD)

        # Approval order doesn't matter — the queue is oldest-submitted first.
        head = next_approved(conn, GUILD)
        assert head is not None and int(head["id"]) == first

        assert mark_posted(conn, first, 55) is True
        # A second mod racing the same queued question loses.
        assert mark_posted(conn, first, 56) is False
        row = get_submission(conn, first)
        assert row is not None
        assert row["state"] == "posted" and row["qotd_id"] == 55
        assert int(next_approved(conn, GUILD)["id"]) == second


def test_mark_posted_refuses_a_pending_submission(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        assert mark_posted(conn, sid, 1) is False  # not approved yet


def test_claim_next_approved_is_exclusive_and_fifo(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        _fund(conn, 200, USER_2)
        first = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        second = submit_sponsor(
            conn, SETTINGS, GUILD, USER_2, "Second question goes here?"
        ).submission_id
        resolve_submission(conn, second, approve=True, resolver_id=MOD)
        resolve_submission(conn, first, approve=True, resolver_id=MOD)

        # Two mods racing /qotd post take *different* questions, never the same
        # one twice — the whole point of claiming before the send.
        a = claim_next_approved(conn, GUILD)
        b = claim_next_approved(conn, GUILD)
        assert a is not None and b is not None
        assert int(a["id"]) == first  # oldest submitted, not oldest approved
        assert int(b["id"]) == second
        assert claim_next_approved(conn, GUILD) is None
        assert get_submission(conn, first)["state"] == "posted"


def test_claim_empty_queue(db):
    with open_db(db) as conn:
        assert claim_next_approved(conn, GUILD) is None


def test_release_claim_returns_it_to_the_queue(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        resolve_submission(conn, sid, approve=True, resolver_id=MOD)
        claimed = claim_next_approved(conn, GUILD)
        assert claimed is not None
        assert claim_next_approved(conn, GUILD) is None  # off the queue

        # A failed send puts it back, with no refund and no double-charge.
        assert release_claim(conn, sid) is True
        row = get_submission(conn, sid)
        assert row["state"] == "approved" and row["posted_at"] is None
        head = next_approved(conn, GUILD)
        assert head is not None and int(head["id"]) == sid
        assert get_balance(conn, GUILD, USER) == 160


def test_release_refuses_once_the_qotd_is_attached(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        resolve_submission(conn, sid, approve=True, resolver_id=MOD)
        claim_next_approved(conn, GUILD)
        attach_qotd(conn, sid, 99)
        # It really ran — releasing it now would re-queue a posted question.
        assert release_claim(conn, sid) is False
        assert get_submission(conn, sid)["state"] == "posted"


def test_posting_never_refunds(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        sid = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        resolve_submission(conn, sid, approve=True, resolver_id=MOD)
        mark_posted(conn, sid, 7)
        assert get_balance(conn, GUILD, USER) == 60
        assert not [k for k in _ledger_kinds(conn) if k[0] == "qotd_sponsor_refund"]


# ── expiry ─────────────────────────────────────────────────────────────


def test_expiry_refunds_pending_only(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        _fund(conn, 200, USER_2)
        stale = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        approved = submit_sponsor(
            conn, SETTINGS, GUILD, USER_2, "Second question goes here?"
        ).submission_id
        resolve_submission(conn, approved, approve=True, resolver_id=MOD)
        conn.execute(
            "UPDATE econ_qotd_submissions SET created_at = ?",
            (NOW - 20 * DAY,),
        )

        expired = expire_stale_submissions(conn, SETTINGS, GUILD, now=NOW)
        assert [int(r["id"]) for r in expired] == [stale]
        assert get_balance(conn, GUILD, USER) == 200  # refunded
        # An approved question is waiting on staff, not the member — it must
        # not time out from under them.
        assert get_balance(conn, GUILD, USER_2) == 160
        head = next_approved(conn, GUILD)
        assert head is not None and int(head["id"]) == approved


def test_expiry_is_idempotent(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        conn.execute(
            "UPDATE econ_qotd_submissions SET created_at = ?", (NOW - 20 * DAY,)
        )
        assert len(expire_stale_submissions(conn, SETTINGS, GUILD, now=NOW)) == 1
        assert expire_stale_submissions(conn, SETTINGS, GUILD, now=NOW) == []
        refunds = [k for k in _ledger_kinds(conn) if k[0] == "qotd_sponsor_refund"]
        assert refunds == [("qotd_sponsor_refund", 40)]
        assert get_balance(conn, GUILD, USER) == 200


def test_expiry_leaves_fresh_submissions_alone(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        conn.execute(
            "UPDATE econ_qotd_submissions SET created_at = ?", (NOW - 2 * DAY,)
        )
        assert expire_stale_submissions(conn, SETTINGS, GUILD, now=NOW) == []
        assert get_balance(conn, GUILD, USER) == 160


def test_expiry_disabled_at_zero_days(db):
    from dataclasses import replace

    never = replace(SETTINGS, qotd_sponsor_expire_days=0)
    with open_db(db) as conn:
        _fund(conn, 200)
        submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        conn.execute(
            "UPDATE econ_qotd_submissions SET created_at = ?", (NOW - 900 * DAY,)
        )
        assert expire_stale_submissions(conn, never, GUILD, now=NOW) == []


def test_expiry_is_guild_scoped(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION)
        conn.execute(
            "UPDATE econ_qotd_submissions SET created_at = ?", (NOW - 20 * DAY,)
        )
        assert expire_stale_submissions(conn, SETTINGS, GUILD + 1, now=NOW) == []
        assert get_balance(conn, GUILD, USER) == 160


# ── listing ────────────────────────────────────────────────────────────


def test_list_submissions_filters_by_state(db):
    with open_db(db) as conn:
        _fund(conn, 200)
        _fund(conn, 200, USER_2)
        a = submit_sponsor(conn, SETTINGS, GUILD, USER, QUESTION).submission_id
        submit_sponsor(conn, SETTINGS, GUILD, USER_2, "Second question goes here?")
        resolve_submission(conn, a, approve=True, resolver_id=MOD)
        assert [int(r["id"]) for r in list_submissions(conn, GUILD, "approved")] == [a]
        assert len(list_submissions(conn, GUILD, "pending")) == 1
        assert len(list_submissions(conn, GUILD)) == 2
