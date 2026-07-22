"""Pure statistics for the Bank Manager Statistics page (spec §9).

Deterministic math over a list of wallet balances / incomes — no discord, no
database. The DB-touching assembly that feeds these lives in
``services/economy_stats_service``.

* :func:`gini` — inequality of a holding distribution, 0 (perfectly equal) to
  ~1 (one holder owns everything).
* :func:`top_share` — the fraction of total currency held by the wealthiest
  ``fraction`` of holders.
* :func:`balance_histogram` — a log-ish fixed-bucket count for the distribution
  bar chart.
* :func:`affordability` — how many days of median daily income each perk price
  costs.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bot_modules.services.economy_service import EconSettings


class BurnRow(TypedDict):
    """One member's lifetime spend, ranked. ``share`` is of the guild's total burn."""

    user_id: str
    burned: int
    share: float
    top_sink: str | None


class HistogramBucket(TypedDict):
    """One balance-histogram bucket: ``[lo, hi]`` (hi None = open-ended) + count."""

    lo: int
    hi: int | None
    count: int

# Fixed lower bounds for the balance histogram. Bucket i spans
# [BUCKETS[i], BUCKETS[i+1] - 1]; the last bucket is open-ended (1000+). The
# leading 0 bound gives 0 its own single-value bucket ([0, 0]).
DEFAULT_BUCKETS: tuple[int, ...] = (0, 1, 10, 50, 100, 250, 500, 1000)

# Negative-amount ledger kinds that are NOT a member spending on something,
# and so don't count as burn. ``transfer_out`` is sideways — the coins land in
# another wallet, nothing leaves the economy. ``qa_void`` is a staff clawback:
# a real removal, but reading it as spending would put someone at the top of a
# "biggest spenders" board for having had a reward revoked. ``casino_stake``
# is gross gambling turnover — a break-even slots player recycles the same
# coins at ~93% RTP, so counting stakes would rank churn above members who
# actually sank currency into perks (the status the board exists to reward).
BURN_EXCLUDED_KINDS: tuple[str, ...] = ("transfer_out", "qa_void", "casino_stake")

# The rentable-perk price fields, in the same set/order the pricing hints use so
# an affordability figure lines up with its suggested-price hint on the dashboard.
PRICE_FIELDS: tuple[str, ...] = (
    "price_role_color",
    "price_role_name",
    "price_role_icon",
    "price_role_gradient",
    "price_role_holographic",
    "price_streak_shield",
    "price_voice_style",
    "price_text_room",
    "price_voice_room",
    "price_quest_reroll",
)


def gini(values: Sequence[int]) -> float:
    """Gini coefficient of a holding distribution; 0.0 for empty/all-equal.

    Rank formula on the ascending sort, 1-indexed::

        G = 2·Σ(i·xᵢ) / (n·Σx) − (n + 1) / n

    Returns 0.0 for an empty list, a single holder, an all-equal set, or a
    total of zero (nothing to be unequal about). Anchored known values:
    ``gini([1,2,3,4,5]) == 4/15`` and ``gini([0,100]) == 0.5``.
    """
    ordered = sorted(values)
    n = len(ordered)
    total = sum(ordered)
    if n == 0 or total <= 0:
        return 0.0
    weighted = sum((i + 1) * x for i, x in enumerate(ordered))
    return (2.0 * weighted) / (n * total) - (n + 1) / n


def top_share(values: Sequence[int], fraction: float = 0.1) -> float:
    """Fraction of the total held by the wealthiest ``fraction`` of holders.

    The top ``ceil(n · fraction)`` values by size (at least one when the list is
    non-empty) divided by the grand total. Returns 0.0 for an empty list or a
    zero total.
    """
    ordered = sorted(values, reverse=True)
    n = len(ordered)
    total = sum(ordered)
    if n == 0 or total <= 0:
        return 0.0
    k = min(n, max(1, math.ceil(n * fraction)))
    return sum(ordered[:k]) / total


def balance_histogram(
    values: Sequence[int], buckets: Sequence[int] = DEFAULT_BUCKETS
) -> list[HistogramBucket]:
    """Bucket ``values`` into ``[{lo, hi, count}]`` over ``buckets`` lower bounds.

    ``buckets`` is an ascending list of lower bounds; bucket ``i`` spans
    ``[buckets[i], buckets[i+1] - 1]`` and the final bucket is open-ended
    (``hi is None``). Values below the first bound are dropped (with the default
    0 bound, nothing is). A value lands in the highest bucket whose lower bound
    it meets.
    """
    bounds = list(buckets)
    counts = [0] * len(bounds)
    for v in values:
        idx = -1
        for i, lo in enumerate(bounds):
            if v >= lo:
                idx = i
            else:
                break
        if idx >= 0:
            counts[idx] += 1
    out: list[HistogramBucket] = []
    for i, lo in enumerate(bounds):
        hi: int | None = bounds[i + 1] - 1 if i + 1 < len(bounds) else None
        out.append({"lo": lo, "hi": hi, "count": counts[i]})
    return out


def burn_leaderboard(
    by_user: Mapping[int, Mapping[str, int]], limit: int = 10
) -> list[BurnRow]:
    """Rank members by lifetime currency *burned*, richest spender first.

    ``by_user`` maps user id → {ledger kind → positive amount spent}. Each
    member's ``share`` is their fraction of the guild's whole burn, not just
    the burn of the rows that survive ``limit`` — a top-10 cut must not
    inflate the percentages it shows.

    Ties break on user id ascending so the table is stable between refreshes.
    Members with nothing burned are dropped; a spender leaderboard with a row
    reading 0 is noise.
    """
    totals = {uid: sum(kinds.values()) for uid, kinds in by_user.items()}
    grand = sum(totals.values())
    ranked = sorted(
        ((uid, t) for uid, t in totals.items() if t > 0),
        key=lambda pair: (-pair[1], pair[0]),
    )
    out: list[BurnRow] = []
    for uid, burned in ranked[: max(limit, 0)]:
        kinds = by_user[uid]
        top_sink = max(kinds, key=lambda k: (kinds[k], k)) if kinds else None
        out.append(
            {
                "user_id": str(uid),
                "burned": burned,
                "share": round(burned / grand, 4) if grand > 0 else 0.0,
                "top_sink": top_sink,
            }
        )
    return out


def affordability(
    median_daily_income: float, settings: EconSettings
) -> dict[str, float]:
    """Days of median daily income each perk price costs (``price / income``).

    Rounded to 1 decimal place, keyed by the ``price_*`` settings field. Returns
    ``{}`` when ``median_daily_income <= 0`` (no earning basis to divide by).
    """
    if median_daily_income <= 0:
        return {}
    return {
        field: round(getattr(settings, field) / median_daily_income, 1)
        for field in PRICE_FIELDS
    }
