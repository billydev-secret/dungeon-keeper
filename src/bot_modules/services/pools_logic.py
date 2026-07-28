"""Pure math for Pools, the parimutuel prediction market.

See docs/plans/casino-classics-and-prediction-market.md Stage 2.

Nothing here touches the database or the ledger — the service layer feeds
these functions rows and they return numbers. That split matters more than
usual for this feature, because the same numbers reach two surfaces (the
settlement and the chart) and they must not be able to disagree: both call
the functions here.

Two properties are load-bearing and pinned by tests rather than comments:

* **The takeout is a residual, never a rate applied up front.** Each payout
  is floored, and whatever the flooring leaves behind joins the takeout. So
  the house can never end a round short, and integer dust is burned rather
  than minted. Stating ``sum(stakes) == sum(payouts) + takeout`` would be
  vacuous — that is the definition of takeout rearranged — so the tests
  assert the things that can actually fail: nothing is minted, dust only
  ever burns, and the realised rate stays within a Petal per winner of the
  configured one.
* **The line is a half-integer.** The median of an odd count of integer
  observations is itself an integer, so an exact hit would have no defined
  winner. Offsetting by +0.5 removes the tie case entirely rather than
  needing a push rule.
"""

from __future__ import annotations

import statistics
from typing import NamedTuple

# Sides. Stored in casino_pools_bets.side; also the bet-bucket keys.
OVER = "over"
UNDER = "under"
SIDES = (OVER, UNDER)

# Completed days of metric history required before a round may open. Also
# the moving-average window, so the overlay is defined exactly where the
# line is.
HISTORY_DAYS = 7

DEFAULT_TAKEOUT_PCT = 5


class PoolSplit(NamedTuple):
    """Stakes on each side of an open round."""

    over: int
    under: int

    @property
    def total(self) -> int:
        return self.over + self.under

    def pool_for(self, side: str) -> int:
        return self.over if side == OVER else self.under


class Settlement(NamedTuple):
    """The result of settling a round.

    ``payouts`` is per-bet and index-aligned with the bets passed in, so a
    caller can zip it straight back onto its rows. ``takeout`` is what gets
    burned — the residual, including flooring dust.
    """

    payouts: list[int]
    takeout: int
    winning_side: str | None  # None = void (nothing to settle)
    void: bool


def pool_split(bets: list[dict]) -> PoolSplit:
    """Total staked per side."""
    over = sum(int(b["amount"]) for b in bets if str(b["side"]) == OVER)
    under = sum(int(b["amount"]) for b in bets if str(b["side"]) == UNDER)
    return PoolSplit(over, under)


def implied_probability(split: PoolSplit) -> float | None:
    """Market-implied probability of OVER, as a 0..1 pool share.

    None when nothing is staked — an empty market has no opinion, and
    rendering 50% would be inventing one.
    """
    if split.total <= 0:
        return None
    return split.over / split.total


def winning_side(result: int, line: float) -> str:
    """Which side a settled metric value pays.

    The line is a half-integer, so this is total — there is no tie branch,
    and a caller cannot reach one.
    """
    return OVER if result > line else UNDER


def is_void(split: PoolSplit) -> bool:
    """A round with an empty side has no counterparty and must refund.

    Covers the zero-bet and single-bettor cases too. Taking a takeout from
    a one-sided pool would be a pure tax on whoever showed up, and at
    13-18 bettors a day these rounds are routine rather than exceptional.
    """
    return split.over <= 0 or split.under <= 0


def settle(
    bets: list[dict], result: int, line: float, takeout_pct: int
) -> Settlement:
    """Split the pool pro-rata among the winning side.

    ``payout_i = floor(kappa * total * stake_i / winningPool)``, and the
    takeout is whatever is left. Losing stakes pay the winners; they do NOT
    skim to the jackpot the way every other game's losses do, and the
    takeout is burned rather than fed to the pot, because the pot re-mints
    what it holds.
    """
    split = pool_split(bets)
    if is_void(split):
        return Settlement([0] * len(bets), 0, None, True)

    side = winning_side(result, line)
    win_pool = split.pool_for(side)
    kept = 100 - _clamp_pct(takeout_pct)

    payouts: list[int] = []
    for bet in bets:
        if str(bet["side"]) != side:
            payouts.append(0)
            continue
        # Multiply before dividing: integer maths throughout, so the only
        # rounding is the single floor at the end.
        payouts.append(int(bet["amount"]) * split.total * kept // (win_pool * 100))

    return Settlement(payouts, split.total - sum(payouts), side, False)


def _clamp_pct(pct: int) -> int:
    return max(0, min(100, int(pct)))


def derive_line(history: list[int]) -> float | None:
    """The settlement line: median of the trailing window, offset by +0.5.

    ``history`` is the completed daily net-change values, oldest first. Only
    the last HISTORY_DAYS are used. None when there is not enough history —
    the caller must not open a round in that case, rather than guessing a
    line off a partial series.

    The median self-recalibrates: a step change in the economy is absorbed
    within a week without an admin touching a dial.
    """
    if len(history) < HISTORY_DAYS:
        return None
    return statistics.median(history[-HISTORY_DAYS:]) + 0.5


class Candle(NamedTuple):
    """One day of the circulation series, in OHLC form.

    Honest OHLC, not decoration: the cumulative sum of the ledger IS total
    circulation, so ``close - open`` is exactly the day's net change — the
    quantity being bet on. Wicks are real intraday extremes of the running
    level.
    """

    day: str
    open: int
    high: int
    low: int
    close: int
    volume: int

    @property
    def body(self) -> int:
        return self.close - self.open

    @property
    def up(self) -> bool:
        return self.close >= self.open


def build_candles(rows: list[tuple[str, int, int, int, int]]) -> list[Candle]:
    """Assemble candles from ``(day, low, high, close, volume)`` rows.

    Each day's open is the previous day's close, so the series is
    continuous — a gap would imply Petals appeared or vanished between
    days, which cannot happen. The first day opens at its own low, since
    there is no prior close to carry.
    """
    candles: list[Candle] = []
    prev_close: int | None = None
    for day, low, high, close, volume in rows:
        opened = low if prev_close is None else prev_close
        candles.append(
            Candle(day, opened, max(high, opened), min(low, opened), close, volume)
        )
        prev_close = close
    return candles


def median_band(
    values: list[int], window: int = HISTORY_DAYS
) -> tuple[list[float | None], list[float | None]]:
    """Rolling median and standard deviation, for the chart overlay.

    Returns ``(median, sigma)`` index-aligned with ``values``, with None in
    the warmup positions. The overlay must not draw where the line itself
    would be undefined — an average over three days implies a signal that
    the spec explicitly refuses to open a round on.
    """
    med: list[float | None] = []
    sig: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            med.append(None)
            sig.append(None)
            continue
        w = values[i + 1 - window:i + 1]
        med.append(statistics.median(w))
        sig.append(statistics.pstdev(w))
    return med, sig


def probability_series(
    bets: list[dict], opened_at: float, closes_at: float
) -> list[tuple[float, float]]:
    """The implied-probability path across the betting window.

    Returns ``(x, p)`` points where x is 0..1 across the window and p is the
    pool-implied probability of OVER *after* each bet landed. This is the
    market chart's whole content: a parimutuel pool share moving as money
    arrives is the same object as a price on a prediction market.

    Bets are replayed in placement order, so the path is reconstructed from
    the stored rows rather than needing a separate time series — there is
    nothing to keep in sync and nothing to lose on a restart.
    """
    span = max(1.0, closes_at - opened_at)
    over = under = 0
    out: list[tuple[float, float]] = []
    for bet in sorted(bets, key=lambda b: (float(b["created_at"]), int(b["id"]))):
        if str(bet["side"]) == OVER:
            over += int(bet["amount"])
        else:
            under += int(bet["amount"])
        total = over + under
        if total <= 0:
            continue
        x = min(1.0, max(0.0, (float(bet["created_at"]) - opened_at) / span))
        out.append((x, over / total))
    return out


def describe_side(side: str) -> str:
    return "Over" if side == OVER else "Under"


def format_line(line: float) -> str:
    """The line, rendered for copy. Always shows the .5 — it is the reason
    a push is impossible, so hiding it invites 'what if it lands exactly'."""
    return f"{line:,.1f}"
