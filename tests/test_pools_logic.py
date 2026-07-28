"""Tests for services/pools_logic.py — the parimutuel maths.

The properties that carry weight here are conservation ones. Asserting
``sum(stakes) == sum(payouts) + takeout`` would be vacuous, since that is
how takeout is defined — so these pin the things that can actually fail:
nothing is minted, flooring dust only ever burns, the realised takeout
tracks the configured rate, and the line can never be hit exactly.
"""

from __future__ import annotations

import pytest

from bot_modules.services import pools_logic as L


def _bets(*pairs: tuple[str, int]) -> list[dict]:
    return [
        {"id": i + 1, "side": side, "amount": amount}
        for i, (side, amount) in enumerate(pairs)
    ]


# ── the pool ───────────────────────────────────────────────────────────


def test_split_and_implied_probability():
    split = L.pool_split(_bets((L.OVER, 300), (L.UNDER, 100), (L.OVER, 100)))
    assert (split.over, split.under, split.total) == (400, 100, 500)
    assert L.implied_probability(split) == pytest.approx(0.8)


def test_empty_market_has_no_opinion():
    # Not 50% — rendering a number for an empty pool would be inventing one.
    assert L.implied_probability(L.pool_split([])) is None


# ── the line ───────────────────────────────────────────────────────────


def test_line_is_median_plus_half():
    assert L.derive_line([100, 200, 300, 400, 500, 600, 700]) == 400.5


def test_line_uses_only_the_trailing_window():
    # The 0s are older than the window and must not drag the line down.
    history = [0] * 20 + [1000, 1100, 1200, 1300, 1400, 1500, 1600]
    assert L.derive_line(history) == 1300.5


def test_line_needs_a_full_week_of_history():
    assert L.derive_line([1, 2, 3, 4, 5, 6]) is None
    assert L.derive_line([]) is None
    assert L.derive_line([1, 2, 3, 4, 5, 6, 7]) is not None


@pytest.mark.parametrize("result", [3904, 3905, 0, -5000, 999_999])
def test_no_result_can_tie_the_line(result):
    """A half-integer line makes a push unreachable, so there is no branch
    for it to fall through. The median of integers is an integer, which is
    exactly why the +0.5 exists."""
    line = L.derive_line([3904] * 7)
    assert line == 3904.5
    assert L.winning_side(result, line) in L.SIDES


def test_winning_side_straddles_the_line():
    assert L.winning_side(3905, 3904.5) == L.OVER
    assert L.winning_side(3904, 3904.5) == L.UNDER


# ── settlement ─────────────────────────────────────────────────────────


def test_pro_rata_split_among_winners():
    bets = _bets((L.OVER, 100), (L.OVER, 50), (L.UNDER, 200))
    s = L.settle(bets, result=5000, line=3904.5, takeout_pct=5)
    assert s.winning_side == L.OVER
    assert s.payouts[2] == 0                     # the loser gets nothing
    # 350 pool, 95% kept = 332.5; over-side holds 150, split 2:1.
    assert s.payouts == [221, 110, 0]
    assert s.payouts[0] == pytest.approx(s.payouts[1] * 2, abs=1)


@pytest.mark.parametrize(
    "bets",
    [
        _bets((L.OVER, 10)),                       # one bettor, one side
        _bets((L.OVER, 10), (L.OVER, 20)),         # all on one side
        [],                                        # nobody played
        _bets((L.UNDER, 5)),
    ],
)
def test_one_sided_pool_voids_rather_than_taking_a_cut(bets):
    """No counterparty means there is nothing to pay winners out of, and
    skimming would tax the only people who showed up. At 13-18 bettors a
    day these rounds are routine."""
    s = L.settle(bets, result=5000, line=3904.5, takeout_pct=5)
    assert s.void is True
    assert s.winning_side is None
    assert s.takeout == 0
    assert all(p == 0 for p in s.payouts)


# ── conservation ───────────────────────────────────────────────────────


@pytest.mark.parametrize("takeout_pct", [0, 1, 5, 10, 50])
@pytest.mark.parametrize(
    "bets",
    [
        _bets((L.OVER, 100), (L.UNDER, 100)),
        _bets((L.OVER, 7), (L.UNDER, 3)),               # awkward ratios
        _bets((L.OVER, 1), (L.UNDER, 999)),             # lopsided
        _bets((L.OVER, 3), (L.OVER, 3), (L.OVER, 3), (L.UNDER, 1)),
        _bets(*[(L.OVER, 1)] * 17, *[(L.UNDER, 1)] * 13),
    ],
)
def test_nothing_is_minted(bets, takeout_pct):
    """The house can never end a round short: what goes out is at most
    what came in, whatever the ratios and whatever the rate."""
    s = L.settle(bets, result=5000, line=3904.5, takeout_pct=takeout_pct)
    staked = sum(b["amount"] for b in bets)
    assert sum(s.payouts) <= staked
    assert s.takeout >= 0
    assert sum(s.payouts) + s.takeout == staked


@pytest.mark.parametrize(
    "bets",
    [
        _bets((L.OVER, 7), (L.UNDER, 3)),
        _bets((L.OVER, 3), (L.OVER, 3), (L.OVER, 3), (L.UNDER, 1)),
        _bets(*[(L.OVER, 11)] * 9, (L.UNDER, 5)),
    ],
)
def test_dust_only_ever_burns(bets):
    """Flooring each payout can only push the residual up, never down —
    so integer dust is destroyed rather than conjured."""
    s = L.settle(bets, result=5000, line=3904.5, takeout_pct=5)
    staked = sum(b["amount"] for b in bets)
    assert s.takeout >= staked * 5 // 100


def test_realised_takeout_tracks_the_configured_rate():
    """Catches a payout formula that silently drifts off kappa — the
    residual stays within a Petal per winner of the nominal rate."""
    bets = _bets(*[(L.OVER, 100)] * 10, *[(L.UNDER, 100)] * 10)
    s = L.settle(bets, result=5000, line=3904.5, takeout_pct=5)
    winners = sum(1 for b in bets if b["side"] == L.OVER)
    assert abs(s.takeout - 2000 * 5 // 100) <= winners


def test_zero_takeout_returns_the_whole_pool():
    bets = _bets((L.OVER, 100), (L.UNDER, 100))
    s = L.settle(bets, result=5000, line=3904.5, takeout_pct=0)
    assert sum(s.payouts) == 200
    assert s.takeout == 0


def test_a_single_winner_takes_the_pool_less_takeout():
    bets = _bets((L.OVER, 50), (L.UNDER, 200), (L.UNDER, 300))
    s = L.settle(bets, result=9999, line=3904.5, takeout_pct=5)
    assert s.payouts == [522, 0, 0]              # floor(550 * 0.95)
    assert s.takeout == 28


# ── candles ────────────────────────────────────────────────────────────


def test_candles_are_continuous():
    """Each open is the prior close. A gap would mean Petals appeared or
    vanished between days, which cannot happen."""
    rows = [
        ("2026-07-01", 0, 100, 90, 5),
        ("2026-07-02", 80, 220, 200, 9),
        ("2026-07-03", 150, 260, 160, 4),
    ]
    candles = L.build_candles(rows)
    assert [c.open for c in candles] == [0, 90, 200]
    assert [c.close for c in candles] == [90, 200, 160]
    assert [c.body for c in candles] == [90, 110, -40]
    assert [c.up for c in candles] == [True, True, False]


def test_candle_wicks_cover_the_open():
    """A day that only ever fell still has its open as the high — the
    level was genuinely there at the start of the day."""
    candles = L.build_candles([
        ("2026-07-01", 0, 100, 100, 3),
        ("2026-07-02", 60, 90, 70, 3),
    ])
    assert candles[1].high == 100
    assert candles[1].low == 60


def test_first_candle_opens_at_its_own_low():
    candles = L.build_candles([("2026-07-01", 5, 50, 40, 2)])
    assert candles[0].open == 5


def test_empty_series_is_not_an_error():
    assert L.build_candles([]) == []


# ── chart overlay ──────────────────────────────────────────────────────


def test_band_is_undefined_during_warmup():
    """The overlay must not draw where the line itself would be undefined —
    a 3-day average implies a signal the spec refuses to open a round on."""
    med, sig = L.median_band([10, 20, 30, 40, 50, 60, 70, 80], window=7)
    assert med[:6] == [None] * 6
    assert sig[:6] == [None] * 6
    assert med[6] == 40
    assert med[7] == 50


def test_band_sigma_is_zero_for_a_flat_series():
    med, sig = L.median_band([100] * 7, window=7)
    assert med[6] == 100
    assert sig[6] == 0


def test_describe_and_format():
    assert L.describe_side(L.OVER) == "Over"
    assert L.describe_side(L.UNDER) == "Under"
    # The .5 always shows — hiding it invites "what if it lands exactly".
    assert L.format_line(3904.5) == "3,904.5"
