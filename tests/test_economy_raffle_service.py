"""Tests for services/economy_raffle_service.py — the weekly ticket raffle.

The money-critical paths: tickets are a pure burn (no coin ever returns), the
per-member weekly cap, the exactly-once weighted draw at the week roll, and
the free-week voucher covering exactly one rental debit (renewal or first
week of a new rent) before expiring.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.rentals import WEEK_SECONDS
from bot_modules.services.economy_raffle_service import (
    VOUCHER_LIFETIME_DAYS,
    buy_tickets,
    draw_raffle,
    get_draw,
    live_voucher,
    member_tickets,
    raffle_enabled,
    try_redeem_voucher,
    week_totals,
)
from bot_modules.services.economy_rentals_service import bill_rental, rent_perk
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from migrations import apply_migrations_sync

GUILD = 700
USER = 2001
USER_2 = 2002
WEEK = "2026-W29"
NOW = 1_800_000_000.0

SETTINGS = EconSettings(
    enabled=True, raffle_enabled=True, price_raffle_ticket=10,
    raffle_max_tickets=10,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant")


# ── buying tickets ─────────────────────────────────────────────────────


def test_buy_burns_coins_and_counts(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        out = buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 3)
        assert out.price == 30 and out.week_total == 3
        assert get_balance(conn, GUILD, USER) == 70
        out = buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 2)
        assert out.week_total == 5
        assert week_totals(conn, GUILD, WEEK) == (5, 1)


def test_buy_enforces_weekly_cap_and_validation(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 10)
        with pytest.raises(ValueError, match="cap"):
            buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 1)
        with pytest.raises(ValueError, match="at least one"):
            buy_tickets(conn, SETTINGS, GUILD, USER_2, WEEK, 0)
        # A fresh week starts a fresh cap.
        buy_tickets(conn, SETTINGS, GUILD, USER, "2026-W30", 1)


def test_buy_disabled_and_insufficient(db):
    off = EconSettings(enabled=True, raffle_enabled=False)
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="isn't running"):
            buy_tickets(conn, off, GUILD, USER, WEEK, 1)
        assert raffle_enabled(off) is False
        _fund(conn, 5)
        with pytest.raises(ValueError, match="you have 5"):
            buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 1)
        assert member_tickets(conn, GUILD, WEEK, USER) == 0


# ── the draw ───────────────────────────────────────────────────────────


def test_draw_weighted_and_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, 100)
        _fund(conn, 100, user_id=USER_2)
        buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 9)
        buy_tickets(conn, SETTINGS, GUILD, USER_2, WEEK, 1)

        result = draw_raffle(
            conn, GUILD, WEEK, now=NOW, rng=random.Random(42)
        )
        assert result is not None
        assert result.tickets == 10 and result.entrants == 2
        assert result.winner_id in (USER, USER_2)
        assert result.voucher_id is not None
        voucher = live_voucher(conn, GUILD, result.winner_id, now=NOW)
        assert voucher is not None
        assert voucher["source"] == f"raffle:{WEEK}"

        # A replay of the week roll draws nothing and issues nothing.
        assert draw_raffle(conn, GUILD, WEEK, now=NOW + 60) is None
        row = get_draw(conn, GUILD, WEEK)
        assert row is not None and row["drawn_at"] == NOW


def test_draw_seeded_rng_is_ticket_weighted(db):
    # With 9-vs-1 tickets, the heavy holder wins the overwhelming majority of
    # seeded draws — a coarse check that weights actually reach the RNG.
    wins = 0
    for seed in range(50):
        rng = random.Random(seed)
        pick = rng.choices([USER, USER_2], weights=[9, 1])[0]
        wins += pick == USER
    assert wins > 35


def test_zero_ticket_week_records_winnerless_draw(db):
    with open_db(db) as conn:
        result = draw_raffle(conn, GUILD, WEEK, now=NOW)
        assert result is not None and result.winner_id is None
        assert result.voucher_id is None
        assert draw_raffle(conn, GUILD, WEEK) is None  # still exactly-once


# ── the voucher ────────────────────────────────────────────────────────


def _issue_voucher(conn, user_id=USER, *, created=NOW):
    conn.execute(
        "INSERT INTO econ_vouchers (guild_id, user_id, kind, state, source, "
        "created_at, expires_at) VALUES (?, ?, 'free_week', 'issued', 't', ?, ?)",
        (GUILD, user_id, created, created + VOUCHER_LIFETIME_DAYS * 86400),
    )


def test_voucher_covers_renewal_once(db):
    settings = EconSettings(enabled=True)
    with open_db(db) as conn:
        _fund(conn, 200)
        rental = rent_perk(conn, settings, GUILD, USER, "role_color", now=NOW)
        assert get_balance(conn, GUILD, USER) == 150
        _issue_voucher(conn)

        result = bill_rental(conn, settings, rental, NOW + WEEK_SECONDS + 1)
        assert result.action == "charge"
        assert get_balance(conn, GUILD, USER) == 150  # covered, no debit
        voucher = conn.execute(
            "SELECT * FROM econ_vouchers WHERE user_id = ?", (USER,)
        ).fetchone()
        assert voucher["state"] == "redeemed"
        assert voucher["rental_id"] == rental["id"]
        # The 0-amount ledger row narrates the covered renewal.
        zero = conn.execute(
            "SELECT * FROM econ_ledger WHERE amount = 0 AND kind = 'rental'"
        ).fetchone()
        assert zero is not None

        # Next renewal charges normally — the voucher is spent.
        fresh = conn.execute(
            "SELECT * FROM econ_rentals WHERE id = ?", (rental["id"],)
        ).fetchone()
        result = bill_rental(conn, settings, fresh, NOW + 2 * WEEK_SECONDS + 1)
        assert result.action == "charge" and result.charged == 50
        assert get_balance(conn, GUILD, USER) == 100


def test_voucher_covers_first_week_of_new_rent(db):
    settings = EconSettings(enabled=True)
    with open_db(db) as conn:
        _issue_voucher(conn)
        # Zero balance — only the voucher makes this rent possible.
        row = rent_perk(conn, settings, GUILD, USER, "role_name", now=NOW)
        assert row["state"] == "active"
        assert get_balance(conn, GUILD, USER) == 0


def test_expired_voucher_never_redeems(db):
    with open_db(db) as conn:
        _issue_voucher(conn, created=NOW - (VOUCHER_LIFETIME_DAYS + 1) * 86400)
        assert live_voucher(conn, GUILD, USER, now=NOW) is None
        hit = try_redeem_voucher(
            conn, GUILD, USER, rental_id=1, perk="role_color", covered=50,
            now=NOW,
        )
        assert hit is None
        row = conn.execute(
            "SELECT state FROM econ_vouchers WHERE user_id = ?", (USER,)
        ).fetchone()
        assert row["state"] == "expired"  # lazily swept on the way through


def test_buy_tickets_fires_shop_purchase_trigger(db):
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        _fund(conn, 100)
        buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 1)
        row = conn.execute(
            "SELECT 1 FROM econ_kind_activity WHERE guild_id = ? "
            "AND user_id = ? AND kind = 'shop_purchase'",
            (GUILD, USER),
        ).fetchone()
        assert row is not None


def test_concurrent_buys_cannot_exceed_the_weekly_cap(db, monkeypatch):
    """Two simultaneous buys must not jointly overshoot the per-member cap.

    The held-ticket count is read in autocommit, so without the cap enforced
    inside the upsert both buys clear the Python check and the member carries
    extra weighted odds into the draw for the rest of the week.
    """
    import bot_modules.services.economy_raffle_service as svc

    real_debit = svc.apply_debit
    fired: list[bool] = []

    def racing_debit(conn, *args, **kwargs):
        # A second buy lands while the first is mid-purchase.
        if not fired:
            fired.append(True)
            with pytest.raises(ValueError, match="cap"):
                svc.buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 6)
        return real_debit(conn, *args, **kwargs)

    monkeypatch.setattr(svc, "apply_debit", racing_debit)

    with open_db(db) as conn:
        _fund(conn, 500)
        buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 6)  # cap is 10

        assert fired, "the racing buy never fired — test is not exercising the race"
        assert member_tickets(conn, GUILD, WEEK, USER) == 6
        assert get_balance(conn, GUILD, USER) == 440  # charged once, 6 × 10


def test_failed_debit_leaves_no_free_tickets(db):
    with open_db(db) as conn:
        _fund(conn, 5)  # not enough for even one ticket at 10
        with pytest.raises(ValueError, match="you have 5"):
            buy_tickets(conn, SETTINGS, GUILD, USER, WEEK, 1)
        assert member_tickets(conn, GUILD, WEEK, USER) == 0
        assert get_balance(conn, GUILD, USER) == 5
