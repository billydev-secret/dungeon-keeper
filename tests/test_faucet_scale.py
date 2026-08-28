"""faucet_scale_pct — one rate over every earned faucet.

Retuning the economy meant editing ~14 dials, which is why the 2026-07-30
retune went stale when the member base grew (per-earner minting fell 41%,
headcount rose 59%, the float kept climbing). This is the single dial, and
what matters is exactly which credits it may and may not touch: shaving a
casino payout, an incoming transfer, an admin grant or a refund would take
coins that are not the guild's to take.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.kinds import NON_FAUCET_KINDS, UNSCALED_CREDIT_KINDS
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD, USER = 900, 4001


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrated_db(path)
    with open_db(path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    return path


def test_defaults_to_a_no_op(db):
    with open_db(db) as conn:
        assert load_econ_settings(conn, GUILD).faucet_scale_pct == 100
        assert apply_credit(conn, GUILD, USER, 50, "quest") == 50


@pytest.mark.parametrize(
    ("pct", "amount", "expected"),
    [
        pytest.param(50, 50, 25, id="half"),
        pytest.param(75, 40, 30, id="three-quarters"),
        pytest.param(150, 40, 60, id="above-100-scales-up"),
        pytest.param(33, 10, 3, id="rounds-down"),
        # A rate is not an off-switch: a faucet that would round away still
        # pays 1, and each faucet keeps its own zero/enable dial.
        pytest.param(1, 10, 1, id="never-rounds-to-nothing"),
        pytest.param(0, 500, 1, id="zero-still-pays-the-floor"),
    ],
)
def test_scales_earned_income(db, pct, amount, expected):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"faucet_scale_pct": pct})
        assert apply_credit(conn, GUILD, USER, amount, "quest") == expected


@pytest.mark.parametrize("kind", sorted(UNSCALED_CREDIT_KINDS))
def test_never_touches_money_that_is_not_earned_income(db, kind):
    """Sideways money, admin grants and refunds arrive intact at any rate."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"faucet_scale_pct": 10})
        assert apply_credit(conn, GUILD, USER, 200, kind) == 200


def test_every_non_faucet_kind_is_unscaled(db):
    # Derived, not restated: an escrow pair added to NON_FAUCET_KINDS is
    # covered here the day it lands, which is the asymmetry that once made a
    # cancelled bounty read as a mint.
    assert set(NON_FAUCET_KINDS) <= UNSCALED_CREDIT_KINDS


def test_scale_rides_after_the_booster(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"faucet_scale_pct": 50})
        # 100 -> ceil(100 * 1.5) = 150 boosted, then halved by the guild rate.
        assert apply_credit(
            conn, GUILD, USER, 100, "quest", booster=True, multiplier=1.5
        ) == 75


def test_caller_may_pass_a_preloaded_rate(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"faucet_scale_pct": 100})
        assert apply_credit(conn, GUILD, USER, 80, "quest", scale_pct=25) == 20


def test_the_wallet_and_ledger_agree_with_what_was_scaled(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"faucet_scale_pct": 40})
        credited = apply_credit(conn, GUILD, USER, 100, "quest")
        assert credited == 40
        assert get_balance(conn, GUILD, USER) == 40
        row = conn.execute(
            "SELECT amount FROM econ_ledger WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()
        # The ledger must record what was paid, not what was asked for —
        # the tuning report sums this column to measure the faucet.
        assert int(row["amount"]) == 40
