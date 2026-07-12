"""Pure economy-faucet math — no discord, no database.

Guild-local day arithmetic, the login streak/grace evaluator (spec §3.1),
payout amounts, and the XP→currency conversion. Everything here is
deterministic on its inputs so the subtle streak rules stay table-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

GRACE_WINDOW_DAYS = 7


def local_day_for(ts: float, offset_hours: float) -> str:
    """Return the guild-local calendar day ("YYYY-MM-DD") for an epoch time."""
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def local_day_bounds(local_day: str, offset_hours: float) -> tuple[float, float]:
    """Return the [start, end) epoch bounds of a guild-local day."""
    tz = timezone(timedelta(hours=offset_hours))
    day = date.fromisoformat(local_day)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


@dataclass(frozen=True)
class LoginEval:
    new_streak: int
    grace_consumed: bool
    reset: bool
    grace_covers_day: str | None


def evaluate_login(
    *,
    today: str,
    last_login_day: str | None,
    current_streak: int,
    last_grace_day: str | None,
) -> LoginEval:
    """Evaluate a first-login-of-the-day against the streak/grace rules.

    Spec §3.1: a consecutive day extends the streak. A single missed day is
    bridged silently by grace if no grace was consumed in the rolling 7 local
    days before the missed day (anchored on ``last_grace_day``) — the streak
    continues as if unbroken. A second miss inside that window, or a gap of
    two or more days, resets the streak to 1. Callers never call on a repeat
    same-day login (the econ_logins dedup row gates that), but a non-positive
    gap is handled as a no-op defensively.
    """
    if last_login_day is None:
        return LoginEval(
            new_streak=1, grace_consumed=False, reset=False, grace_covers_day=None
        )

    gap = (date.fromisoformat(today) - date.fromisoformat(last_login_day)).days
    if gap <= 0:
        return LoginEval(
            new_streak=max(current_streak, 1),
            grace_consumed=False,
            reset=False,
            grace_covers_day=None,
        )
    if gap == 1:
        return LoginEval(
            new_streak=current_streak + 1,
            grace_consumed=False,
            reset=False,
            grace_covers_day=None,
        )
    if gap == 2:
        missed = date.fromisoformat(last_login_day) + timedelta(days=1)
        grace_available = (
            last_grace_day is None
            or (missed - date.fromisoformat(last_grace_day)).days >= GRACE_WINDOW_DAYS
        )
        if grace_available:
            return LoginEval(
                new_streak=current_streak + 1,
                grace_consumed=True,
                reset=False,
                grace_covers_day=missed.isoformat(),
            )
    return LoginEval(
        new_streak=1, grace_consumed=False, reset=True, grace_covers_day=None
    )


def login_amount(streak: int, base: int, bonus_cap: int) -> int:
    """Daily login payout: base + 1/day streak bonus, bonus capped at ``bonus_cap``."""
    bonus = min(max(streak - 1, 0), max(bonus_cap, 0))
    return base + bonus


def milestone_amount(streak: int, s: EconSettings) -> int:
    """Milestone bonus paid ON the day the streak hits the mark, else 0."""
    if streak == 7:
        return s.milestone_day7
    if streak == 30:
        return s.milestone_day30
    if streak == 100:
        return s.milestone_day100
    if streak > 100 and streak % 100 == 0:
        return s.milestone_per_100
    return 0


def convert_xp(xp: float, carry: float, xp_per_coin: float) -> tuple[int, float]:
    """Convert a day's XP (plus carried remainder) to whole coins.

    Floor division; the fractional remainder carries forward. Never yields
    negative values, and a non-positive rate mints nothing — the whole total
    carries so no XP is lost while the rate is misconfigured.
    """
    total = max(0.0, xp) + max(0.0, carry)
    if xp_per_coin <= 0:
        return 0, total
    coins = int(total // xp_per_coin)
    remainder = round(total - coins * xp_per_coin, 6)
    return coins, max(0.0, remainder)
