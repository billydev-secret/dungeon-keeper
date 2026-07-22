"""Pure metrics math — no discord, no database (spec §9).

The weekly-rollup helpers that stay deterministic on their inputs: ISO-week
epoch bounds and day range (year-rollover-safe via ``date.fromisocalendar``),
the median / nearest-rank p90 income statistics, the faucet-mix share split,
and the pricing hints. The DB-touching rollup that feeds these lives in
``services/economy_metrics_service``.
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import TYPE_CHECKING

from bot_modules.economy.logic import local_day_bounds

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bot_modules.services.economy_service import EconSettings

# Ledger credit ``kind`` → faucet group. Every minted (positive, non-transfer_in)
# kind maps to exactly one group, so the shares sum to ~1.0 (modulo rounding).
FAUCET_GROUPS: dict[str, str] = {
    "login": "logins",
    "milestone": "logins",
    "conversion": "activity",
    "quest": "quests",
    "quest_community": "quests",
    "qa_reward": "quests",
    "game_participation": "games",
    "game_win": "games",
    "qotd": "games",
    # casino_payout is deliberately NOT here (the wager_payout precedent):
    # gross winnings are ~93-97% of turnover, so mapping them to "games"
    # would drown the real game faucets in the mix chart and inflate the
    # median-income figure the pricing hints are anchored to.
    "grant": "grants",
}

# Stable group order for a zero-filled faucet mix (matches FAUCET_GROUPS values).
FAUCET_GROUP_NAMES: tuple[str, ...] = ("logins", "activity", "quests", "games", "grants")

# Suggested price ≈ round(median weekly income × factor). Anchored to the spec's
# default price ratios at a median of 100 (role_color 50 → 0.5, etc.). Keyed by
# the EconSettings ``price_*`` field so the dashboard can align hint↔input.
PRICING_FACTORS: dict[str, float] = {
    "price_role_color": 0.5,
    "price_role_name": 0.35,
    "price_role_icon": 0.75,
    "price_role_gradient": 1.2,
    "price_role_holographic": 3.0,
    "price_voice_style": 0.3,
    "price_text_room": 2.0,
    "price_voice_room": 2.0,
    # Consumables, not rentals — priced as impulse buys, not considered
    # purchases like the perks above. The shield runs richer than a reroll:
    # it protects accumulated value (a long streak), not a single board slot.
    "price_quest_reroll": 0.1,
    "price_streak_shield": 0.3,
}


def _parse_iso_week(iso_week: str) -> tuple[int, int]:
    """Split "YYYY-Www" into (iso_year, iso_week_number). Raises ValueError."""
    year_s, _, week_s = iso_week.partition("-W")
    if not year_s or not week_s:
        raise ValueError(f"malformed ISO week: {iso_week!r}")
    return int(year_s), int(week_s)


def iso_week_day_range(iso_week: str) -> tuple[str, str]:
    """Return the (Monday, Sunday) guild-local day strings of an ISO week.

    Year-rollover safe: ``date.fromisocalendar`` resolves the ISO year/week to
    the correct Gregorian dates, so 2020-W53's Monday is 2020-12-28 and 2026-W01
    starts on the right side of the calendar-year boundary.
    """
    year, week = _parse_iso_week(iso_week)
    monday = date.fromisocalendar(year, week, 1)
    sunday = date.fromisocalendar(year, week, 7)
    return monday.isoformat(), sunday.isoformat()


def iso_week_bounds(iso_week: str, offset_hours: float) -> tuple[float, float]:
    """Return the [start, end) epoch bounds of a guild-local ISO week.

    The week spans its Monday 00:00 through the following Monday 00:00 in the
    guild's local timezone (``offset_hours``). Built from the same
    ``local_day_bounds`` the day roll uses, so week and day windows tile exactly.
    """
    monday, sunday = iso_week_day_range(iso_week)
    start, _ = local_day_bounds(monday, offset_hours)
    _, end = local_day_bounds(sunday, offset_hours)
    return start, end


def median_income(incomes: Iterable[float]) -> float:
    """Median weekly income over earners; 0.0 when there are none.

    ``statistics.median`` — the mean of the two middle values on an even count.
    """
    values = list(incomes)
    if not values:
        return 0.0
    return float(statistics.median(values))


def p90_income(incomes: Iterable[float]) -> float:
    """90th-percentile income by the nearest-rank method; 0.0 when empty.

    Nearest-rank on the ascending sort: rank = ceil(0.9 × n), 1-indexed, so
    p90 is an actual observed income (never interpolated). n=1 → the sole value.
    """
    values = sorted(incomes)
    if not values:
        return 0.0
    rank = max(1, math.ceil(0.9 * len(values)))
    return float(values[rank - 1])


def faucet_shares(minted_by_kind: Mapping[str, float], minted: float) -> dict[str, float]:
    """Split total minted into per-group shares (each a 0-1 fraction, 3dp).

    ``minted_by_kind`` is the summed positive credit per ledger ``kind`` (already
    transfer_in-free). Returns ``{}`` when ``minted <= 0`` — there is no mix to
    render. Kinds outside :data:`FAUCET_GROUPS` are ignored (they cannot mint).
    """
    if minted <= 0:
        return {}
    shares = dict.fromkeys(FAUCET_GROUP_NAMES, 0.0)
    for kind, amount in minted_by_kind.items():
        group = FAUCET_GROUPS.get(kind)
        if group is not None:
            shares[group] += amount
    return {group: round(total / minted, 3) for group, total in shares.items()}


def pricing_hints(median_income: float, settings: EconSettings) -> dict[str, int]:  # noqa: ARG001
    """Suggested per-perk prices from the median weekly income (spec §9).

    ``price_<perk> ≈ round(median × factor)`` with factors anchored to the
    spec's default ratios. Returns ``{}`` when ``median <= 0`` (no basis to
    suggest against). Advisory only — the dashboard shows these beside each
    price field with no enforcement. ``settings`` is accepted for signature
    stability (the factors are fixed constants).
    """
    if median_income <= 0:
        return {}
    return {
        field: round(median_income * factor) for field, factor in PRICING_FACTORS.items()
    }
