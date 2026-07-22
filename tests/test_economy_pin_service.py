"""Tests for services/economy_pin_service.py — the paid, mod-approved daily pin.

The money-critical paths: charged-at-submit, refund on every non-live exit
(deny, expiry-while-pending, failed-go-live), refund exactly-once under replay,
and the state guards that keep one pin live per guild and one submission in
flight per member. A pin that actually went live is a completed purchase — it is
NOT refunded when its 24h runs out.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_pin_service import (
    MAX_PIN_LEN,
    PIN_LIFETIME_SECONDS,
    deny,
    expire_live_pins,
    expire_stale_pending,
    get_submission,
    go_live,
    open_submission,
    pin_enabled,
    refund_failed_golive,
    submit_pin,
    take_down,
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
PIN_CH = 5555
NOW = 1_800_000_000.0
DAY = 86400.0

# Enabled = a price AND a destination channel.
SETTINGS = EconSettings(
    enabled=True, price_pin_of_day=30, pin_channel_id=PIN_CH, pin_expire_days=3
)
MESSAGE = "gm gamers ☕ may your crits land and your queues be short"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant", actor_id=MOD)


def _ledger(conn, user_id=USER):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id",
            (GUILD, user_id),
        )
    ]


# ── enablement ─────────────────────────────────────────────────────────


def test_enabled_needs_price_and_channel():
    assert pin_enabled(SETTINGS) is True
    assert pin_enabled(EconSettings(price_pin_of_day=0, pin_channel_id=PIN_CH)) is False
    assert pin_enabled(EconSettings(price_pin_of_day=30, pin_channel_id=0)) is False


# ── submit ─────────────────────────────────────────────────────────────


def test_submit_charges_and_queues(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        assert out.price == 30
        assert get_balance(conn, GUILD, USER) == 70
        row = get_submission(conn, out.submission_id)
        assert row["state"] == "pending"
        assert row["message"] == MESSAGE
        assert _ledger(conn) == [("grant", 100), ("pin_sponsor", -30)]


def test_submit_disabled_raises_and_no_charge(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        with pytest.raises(ValueError, match="isn't enabled"):
            submit_pin(
                conn, EconSettings(pin_channel_id=0), GUILD, USER, MESSAGE
            )
        assert get_balance(conn, GUILD, USER) == 100


def test_submit_insufficient_is_zero_write(db):
    with open_db(db) as conn:
        _fund(conn, 10)  # a pin costs 30
        with pytest.raises(ValueError, match="you have 10"):
            submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        assert get_balance(conn, GUILD, USER) == 10
        assert open_submission(conn, GUILD, USER) is None


def test_submit_too_long_rejected(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        with pytest.raises(ValueError, match=str(MAX_PIN_LEN)):
            submit_pin(conn, SETTINGS, GUILD, USER, "x" * (MAX_PIN_LEN + 1))
        assert get_balance(conn, GUILD, USER) == 100


def test_submit_empty_rejected(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        with pytest.raises(ValueError, match="nothing to pin"):
            submit_pin(conn, SETTINGS, GUILD, USER, "   \n  ")


def test_submit_one_in_flight_per_member(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        with pytest.raises(ValueError, match="already have a pin"):
            submit_pin(conn, SETTINGS, GUILD, USER, "second one")
        # Charged once.
        assert get_balance(conn, GUILD, USER) == 70


# ── deny ───────────────────────────────────────────────────────────────


def test_deny_refunds_and_is_terminal(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        row = deny(conn, out.submission_id, resolver_id=MOD, deny_reason="ad")
        assert row["state"] == "denied"
        assert row["deny_reason"] == "ad"
        assert get_balance(conn, GUILD, USER) == 100  # refunded
        assert _ledger(conn)[-1] == ("pin_sponsor_refund", 30)


def test_deny_only_pending(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        deny(conn, out.submission_id, resolver_id=MOD)
        with pytest.raises(ValueError, match="already denied"):
            deny(conn, out.submission_id, resolver_id=MOD)
        # Not double-refunded.
        assert get_balance(conn, GUILD, USER) == 100


# ── go_live ────────────────────────────────────────────────────────────


def test_go_live_promotes_and_sets_expiry(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        res = go_live(
            conn, out.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=42, now=NOW,
        )
        assert res.superseded is None
        assert res.live["state"] == "live"
        assert res.live["pin_message_id"] == 42
        assert res.live["went_live_at"] == NOW
        assert res.live["expires_at"] == NOW + PIN_LIFETIME_SECONDS
        # Going live does not touch the wallet — it's not a refund.
        assert get_balance(conn, GUILD, USER) == 70


def test_go_live_supersedes_prior_live(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, USER_2)
        first = submit_pin(conn, SETTINGS, GUILD, USER, "first")
        r1 = go_live(
            conn, first.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=11, now=NOW,
        )
        second = submit_pin(conn, SETTINGS, GUILD, USER_2, "second")
        r2 = go_live(
            conn, second.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=22, now=NOW + 10,
        )
        # The new one is live; the old one is superseded and handed back to
        # unpin (its channel/message ids ride along).
        assert r2.live["state"] == "live"
        assert r2.superseded is not None
        assert int(r2.superseded["id"]) == r1.live["id"]
        assert int(r2.superseded["pin_message_id"]) == 11
        # Exactly one live pin remains for the guild.
        live = conn.execute(
            "SELECT COUNT(*) c FROM econ_pin_submissions "
            "WHERE guild_id = ? AND state = 'live'",
            (GUILD,),
        ).fetchone()["c"]
        assert live == 1
        # Neither member was refunded — a superseded pin had its time up.
        assert get_balance(conn, GUILD, USER) == 70


def test_go_live_only_pending(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        deny(conn, out.submission_id, resolver_id=MOD)
        with pytest.raises(ValueError, match="already denied"):
            go_live(
                conn, out.submission_id, resolver_id=MOD,
                pin_channel_id=PIN_CH, pin_message_id=1, now=NOW,
            )


def test_one_live_per_guild_index_enforced(db):
    """The partial unique index is the backstop even if go_live is bypassed."""
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, USER_2)
        a = submit_pin(conn, SETTINGS, GUILD, USER, "a")
        go_live(
            conn, a.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=1, now=NOW,
        )
        b = submit_pin(conn, SETTINGS, GUILD, USER_2, "b")
        # Force a second row to 'live' directly — the index must reject it.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE econ_pin_submissions SET state = 'live' WHERE id = ?",
                (b.submission_id,),
            )


# ── live expiry (no refund) ────────────────────────────────────────────


def test_expire_live_pins_no_refund(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        go_live(
            conn, out.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=99, now=NOW,
        )
        # Not yet due.
        assert expire_live_pins(conn, GUILD, now=NOW + PIN_LIFETIME_SECONDS - 1) == []
        # Past 24h → retired, returned for the caller to unpin, NOT refunded.
        due = expire_live_pins(conn, GUILD, now=NOW + PIN_LIFETIME_SECONDS + 1)
        assert len(due) == 1
        assert int(due[0]["pin_message_id"]) == 99
        assert get_submission(conn, out.submission_id)["state"] == "expired"
        assert get_balance(conn, GUILD, USER) == 70  # no refund
        # The member can pin again once their live one expired.
        assert open_submission(conn, GUILD, USER) is None


def test_take_down_live_no_refund(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        go_live(
            conn, out.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=7, now=NOW,
        )
        row = take_down(conn, out.submission_id, resolver_id=MOD)
        assert row["state"] == "expired"
        assert int(row["pin_message_id"]) == 7
        assert get_balance(conn, GUILD, USER) == 70  # a yank is not a refund
        with pytest.raises(ValueError, match="isn't up"):
            take_down(conn, out.submission_id, resolver_id=MOD)


# ── pending expiry (refund) ────────────────────────────────────────────


def test_expire_stale_pending_refunds(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        # Age the row past the 3-day window.
        conn.execute(
            "UPDATE econ_pin_submissions SET created_at = ? WHERE id = ?",
            (NOW - 4 * DAY, out.submission_id),
        )
        expired = expire_stale_pending(conn, SETTINGS, GUILD, now=NOW)
        assert len(expired) == 1
        assert get_submission(conn, out.submission_id)["state"] == "expired"
        assert get_balance(conn, GUILD, USER) == 100  # refunded
        assert _ledger(conn)[-1] == ("pin_sponsor_refund", 30)


def test_expire_stale_pending_leaves_fresh_and_live(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, USER_2)
        fresh = submit_pin(conn, SETTINGS, GUILD, USER, "fresh")
        live = submit_pin(conn, SETTINGS, GUILD, USER_2, "live one")
        # Stamp both relative to the sweep's clock: fresh is recent, the live one
        # is old but went live (so it must not expire as "stale pending").
        conn.execute(
            "UPDATE econ_pin_submissions SET created_at = ? WHERE id = ?",
            (NOW, fresh.submission_id),
        )
        conn.execute(
            "UPDATE econ_pin_submissions SET created_at = ? WHERE id = ?",
            (NOW - 10 * DAY, live.submission_id),
        )
        go_live(
            conn, live.submission_id, resolver_id=MOD,
            pin_channel_id=PIN_CH, pin_message_id=5, now=NOW,
        )
        expired = expire_stale_pending(conn, SETTINGS, GUILD, now=NOW)
        assert expired == []  # neither a fresh pending nor a live one expires
        assert get_submission(conn, fresh.submission_id)["state"] == "pending"


def test_expire_days_zero_disables_sweep(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        conn.execute(
            "UPDATE econ_pin_submissions SET created_at = ? WHERE id = ?",
            (NOW - 100 * DAY, out.submission_id),
        )
        settings = EconSettings(
            enabled=True, price_pin_of_day=30, pin_channel_id=PIN_CH, pin_expire_days=0
        )
        assert expire_stale_pending(conn, settings, GUILD, now=NOW) == []


# ── failed go-live refund + exactly-once ───────────────────────────────


def test_refund_failed_golive(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        row = get_submission(conn, out.submission_id)
        refund_failed_golive(conn, row)
        fresh = get_submission(conn, out.submission_id)
        assert fresh["state"] == "denied"
        assert get_balance(conn, GUILD, USER) == 100  # refunded


def test_refund_exactly_once_under_replay(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = submit_pin(conn, SETTINGS, GUILD, USER, MESSAGE)
        deny(conn, out.submission_id, resolver_id=MOD)
        # A stale row snapshot replayed through the failed-go-live path must not
        # pay a second time (refunded_at guards it).
        stale = get_submission(conn, out.submission_id)
        refund_failed_golive(conn, stale)
        assert get_balance(conn, GUILD, USER) == 100
        refunds = [k for k, _ in _ledger(conn) if k == "pin_sponsor_refund"]
        assert len(refunds) == 1
