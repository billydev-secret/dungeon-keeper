"""Tests for services/economy_bounty_service.py — crowdfunded, mod-awarded pots.

The money-critical paths: escrow-at-contribute, the rake maths on award (winner
gets pot − floor(pot × rake%), the rake evaporates), refund-every-contributor on
cancel/expire, refunds exactly-once under replay, and the guards (min stake,
max-open, only-open transitions).
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_bounty_service import (
    award_bounty,
    cancel_bounty,
    contribute,
    contributor_count,
    create_bounty,
    expire_bounties,
    get_bounty,
    open_count_for,
    pot_of,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from migrations import apply_migrations_sync

GUILD = 700
A, B, C, W, MOD = 2001, 2002, 2003, 2004, 9001
CH = 555
NOW = 1_800_000_000.0
DAY = 86400.0

SETTINGS = EconSettings(
    enabled=True, bounty_channel_id=CH, bounty_min_stake=10,
    bounty_max_open=3, bounty_expire_days=14, bounty_rake_pct=0,
)


def _s(**over) -> EconSettings:
    base = dict(
        enabled=True, bounty_channel_id=CH, bounty_min_stake=10,
        bounty_max_open=3, bounty_expire_days=14, bounty_rake_pct=0,
    )
    base.update(over)
    return EconSettings(**base)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, user, amount):
    apply_credit(conn, GUILD, user, amount, "grant", actor_id=MOD)


def _ledger(conn, user):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id",
            (GUILD, user),
        )
    ]


def _open(conn, poster=A, *, title="Draw the mascot", desc="", stake=50, settings=SETTINGS):
    return create_bounty(
        conn, settings, GUILD, poster, title=title, description=desc,
        stake=stake, now=NOW,
    ).bounty_id


# ── create ─────────────────────────────────────────────────────────────


def test_create_escrows_opener_stake(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        bid = _open(conn, stake=50)
        assert get_balance(conn, GUILD, A) == 50
        assert pot_of(conn, bid) == 50
        assert contributor_count(conn, bid) == 1
        assert get_bounty(conn, bid)["state"] == "open"
        assert _ledger(conn, A) == [("grant", 100), ("bounty_stake", -50)]


def test_create_disabled_no_channel(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="aren't enabled"):
            _open(conn, settings=_s(bounty_channel_id=0))
        assert get_balance(conn, GUILD, A) == 100


def test_create_below_min_stake(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="at least 10"):
            _open(conn, stake=5)
        assert get_balance(conn, GUILD, A) == 100


def test_create_insufficient_is_zero_write(db):
    with open_db(db) as conn:
        _fund(conn, A, 20)  # wants 50
        with pytest.raises(ValueError, match="you have 20"):
            _open(conn, stake=50)
        assert get_balance(conn, GUILD, A) == 20
        # The bounty row rolls back with the transaction the caller unwinds.
        assert open_count_for(conn, GUILD, A) == 0


def test_create_title_too_short(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="title"):
            _open(conn, title="hi")


def test_create_max_open_cap(db):
    with open_db(db) as conn:
        _fund(conn, A, 1000)
        for _ in range(3):
            _open(conn, stake=10)
        with pytest.raises(ValueError, match="already have 3 open"):
            _open(conn, stake=10)


# ── contribute ─────────────────────────────────────────────────────────


def test_contribute_grows_pot(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        pot = contribute(conn, SETTINGS, GUILD, bid, B, 30, now=NOW)
        assert pot == 80
        assert contributor_count(conn, bid) == 2
        assert get_balance(conn, GUILD, B) == 70


def test_contribute_multiple_from_same_member(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        contribute(conn, SETTINGS, GUILD, bid, B, 20, now=NOW)
        contribute(conn, SETTINGS, GUILD, bid, B, 15, now=NOW)
        assert pot_of(conn, bid) == 85
        assert contributor_count(conn, bid) == 2  # A + B, distinct members


def test_contribute_below_min(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        with pytest.raises(ValueError, match="at least 10"):
            contribute(conn, SETTINGS, GUILD, bid, B, 5, now=NOW)


def test_contribute_only_open(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        cancel_bounty(conn, GUILD, bid, resolver_id=MOD, now=NOW)
        with pytest.raises(ValueError, match="cancelled"):
            contribute(conn, SETTINGS, GUILD, bid, B, 20, now=NOW)


# ── award (rake maths) ─────────────────────────────────────────────────


def test_award_pays_winner_no_rake(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        contribute(conn, SETTINGS, GUILD, bid, B, 50, now=NOW)  # pot 100
        res = award_bounty(conn, SETTINGS, GUILD, bid, winner_id=W, resolver_id=MOD, now=NOW)
        assert res.payout == 100 and res.rake == 0
        assert get_balance(conn, GUILD, W) == 100
        assert get_bounty(conn, bid)["state"] == "awarded"
        assert _ledger(conn, W)[-1] == ("bounty_payout", 100)


def test_award_takes_rake_that_evaporates(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50, settings=_s(bounty_rake_pct=10))
        contribute(conn, _s(bounty_rake_pct=10), GUILD, bid, B, 50, now=NOW)  # pot 100
        res = award_bounty(
            conn, _s(bounty_rake_pct=10), GUILD, bid, winner_id=W, resolver_id=MOD, now=NOW
        )
        # floor(100 * 10 / 100) = 10 rake; winner gets 90.
        assert res.rake == 10 and res.payout == 90
        assert get_balance(conn, GUILD, W) == 90
        row = get_bounty(conn, bid)
        assert row["rake_amount"] == 10 and row["payout"] == 90
        # The rake is escrow never credited back — a true burn. Total escrowed
        # 100, only 90 re-entered a wallet.
        stakes = -sum(a for k, a in _ledger(conn, A) if k == "bounty_stake")
        stakes += -sum(a for k, a in _ledger(conn, B) if k == "bounty_stake")
        payouts = sum(a for k, a in _ledger(conn, W) if k == "bounty_payout")
        assert stakes - payouts == 10  # the evaporated rake


def test_award_rake_floors(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        bid = _open(conn, poster=A, stake=55, settings=_s(bounty_rake_pct=10))
        # floor(55 * 10 / 100) = 5 (5.5 floored); winner gets 50.
        res = award_bounty(
            conn, _s(bounty_rake_pct=10), GUILD, bid, winner_id=W, resolver_id=MOD, now=NOW
        )
        assert res.rake == 5 and res.payout == 50


def test_award_empty_pot_rejected(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        bid = _open(conn, poster=A, stake=50)
        cancel_bounty(conn, GUILD, bid, resolver_id=MOD, now=NOW)  # pot back to 0
        with pytest.raises(ValueError, match="already cancelled"):
            award_bounty(conn, SETTINGS, GUILD, bid, winner_id=W, resolver_id=MOD, now=NOW)


def test_award_only_open(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        bid = _open(conn, poster=A, stake=50)
        award_bounty(conn, SETTINGS, GUILD, bid, winner_id=W, resolver_id=MOD, now=NOW)
        with pytest.raises(ValueError, match="already awarded"):
            award_bounty(conn, SETTINGS, GUILD, bid, winner_id=B, resolver_id=MOD, now=NOW)


# ── cancel / expire (refund all) ───────────────────────────────────────


def test_cancel_refunds_every_contributor(db):
    with open_db(db) as conn:
        for u in (A, B, C):
            _fund(conn, u, 100)
        bid = _open(conn, poster=A, stake=50)
        contribute(conn, SETTINGS, GUILD, bid, B, 30, now=NOW)
        contribute(conn, SETTINGS, GUILD, bid, C, 20, now=NOW)
        row, refunded = cancel_bounty(conn, GUILD, bid, resolver_id=MOD, now=NOW)
        assert row["state"] == "cancelled"
        assert set(refunded) == {A, B, C}
        assert get_balance(conn, GUILD, A) == 100
        assert get_balance(conn, GUILD, B) == 100
        assert get_balance(conn, GUILD, C) == 100
        assert pot_of(conn, bid) == 0  # every contribution refunded


def test_cancel_refund_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        bid = _open(conn, poster=A, stake=50)
        cancel_bounty(conn, GUILD, bid, resolver_id=MOD, now=NOW)
        with pytest.raises(ValueError, match="already cancelled"):
            cancel_bounty(conn, GUILD, bid, resolver_id=MOD, now=NOW)
        # Not double-refunded.
        assert get_balance(conn, GUILD, A) == 100
        assert [k for k, _ in _ledger(conn, A)].count("bounty_refund") == 1


def test_expire_refunds_and_marks(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        contribute(conn, SETTINGS, GUILD, bid, B, 30, now=NOW)
        # Not due yet.
        assert expire_bounties(conn, SETTINGS, GUILD, now=NOW + 13 * DAY) == []
        # Past the 14-day window.
        exp = expire_bounties(conn, SETTINGS, GUILD, now=NOW + 15 * DAY)
        assert len(exp) == 1
        assert set(exp[0].refunded_user_ids) == {A, B}
        assert get_bounty(conn, bid)["state"] == "expired"
        assert get_balance(conn, GUILD, A) == 100
        assert get_balance(conn, GUILD, B) == 100


def test_expire_days_zero_disables(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        bid = _open(conn, poster=A, stake=50, settings=_s(bounty_expire_days=0))
        # expires_at is NULL, so nothing is ever due.
        assert get_bounty(conn, bid)["expires_at"] is None
        assert expire_bounties(conn, _s(bounty_expire_days=0), GUILD, now=NOW + 999 * DAY) == []


# ── award / chip-in races ──────────────────────────────────────────────
# Both directions of the race lose real coins: the state read in contribute()
# and the pot read in award_bounty() both ran outside the transaction their
# writes start, so a chip-in could land on a bounty that had already paid.


def test_chip_in_racing_an_award_is_refused_not_swallowed(db, monkeypatch):
    """An award committing mid-chip-in must refuse the chip-in, not eat it."""
    import bot_modules.services.economy_bounty_service as svc

    real_get = svc.get_bounty
    fired: list[bool] = []

    def racing_get_bounty(conn, bounty_id):
        row = real_get(conn, bounty_id)
        # The mod's award commits right after contribute() reads the state —
        # the window the open-check used to sit outside of.
        if not fired:
            fired.append(True)
            award_bounty(
                conn, SETTINGS, GUILD, bounty_id,
                winner_id=W, resolver_id=MOD, now=NOW,
            )
        return row

    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)

        monkeypatch.setattr(svc, "get_bounty", racing_get_bounty)
        with pytest.raises(ValueError, match="just closed"):
            contribute(conn, SETTINGS, GUILD, bid, B, 30, now=NOW)

        assert fired, "the racing award never fired — test is not exercising the race"
        # B keeps every coin: nothing escrowed into an already-awarded bounty.
        assert get_balance(conn, GUILD, B) == 100
        assert contributor_count(conn, bid) == 1


def test_award_pays_out_a_chip_in_that_lands_just_before_it(db):
    """The pot is read after the claim, so a contribution can't slip behind it."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        bid = _open(conn, poster=A, stake=50)
        contribute(conn, SETTINGS, GUILD, bid, B, 30, now=NOW)

        res = award_bounty(
            conn, SETTINGS, GUILD, bid, winner_id=W, resolver_id=MOD, now=NOW,
        )

        # Whole pot reaches the winner; nothing is stranded in escrow.
        assert res.payout == 80
        assert get_balance(conn, GUILD, W) == 80
        assert int(get_bounty(conn, bid)["payout"]) == 80
