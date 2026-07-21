"""Pure economy-faucet math — no discord, no database.

Guild-local day arithmetic, the login streak/grace evaluator (spec §3.1),
payout amounts, and the XP→currency conversion. Everything here is
deterministic on its inputs so the subtle streak rules stay table-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

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
    shield_consumed: bool = False


def evaluate_login(
    *,
    today: str,
    last_login_day: str | None,
    current_streak: int,
    last_grace_day: str | None,
    shields_held: int = 0,
) -> LoginEval:
    """Evaluate a first-login-of-the-day against the streak/grace/shield rules.

    Spec §3.1: a consecutive day extends the streak. Each missed day needs one
    cover for the streak to survive: the free grace covers ONE missed day if no
    grace was consumed in the rolling 7 local days before it (anchored on
    ``last_grace_day``), and a purchased streak shield (sinks round 3, stage 2)
    covers one more. Covers are consumed grace-first — with the 1-shield cap
    that means a 2-day gap survives on grace *or* shield, a 3-day gap only
    with both, and a 4+ day gap always resets to 1. A shield is only consumed
    when it actually saves the streak — a hopeless gap leaves it held.
    Callers never call on a repeat same-day login (the econ_logins dedup row
    gates that), but a non-positive gap is handled as a no-op defensively.
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
    missed_days = gap - 1
    first_missed = date.fromisoformat(last_login_day) + timedelta(days=1)
    grace_available = (
        last_grace_day is None
        or (first_missed - date.fromisoformat(last_grace_day)).days
        >= GRACE_WINDOW_DAYS
    )
    covers = (1 if grace_available else 0) + min(max(shields_held, 0), 1)
    if missed_days <= covers:
        # Consume grace first (anchoring its rolling window on the day it
        # covered), then the shield for the remainder.
        use_grace = grace_available
        use_shield = missed_days > (1 if use_grace else 0)
        return LoginEval(
            new_streak=current_streak + 1,
            grace_consumed=use_grace,
            reset=False,
            grace_covers_day=first_missed.isoformat() if use_grace else None,
            shield_consumed=use_shield,
        )
    return LoginEval(
        new_streak=1, grace_consumed=False, reset=True, grace_covers_day=None
    )


def resolve_notify_toggle(*, role_id: int, member_role_ids: Collection[int]) -> str:
    """What the guide panel's 🔔 button should do for this member.

    ``"unconfigured"`` when the guild has no opt-in role set (the button is
    inert rather than silently doing nothing), else ``"remove"`` for a member
    who already holds it and ``"grant"`` for one who doesn't. The role is a
    DM preference only — it gates no channel and no payout — so this never
    consults anything but role membership.
    """
    if not role_id:
        return "unconfigured"
    return "remove" if role_id in member_role_ids else "grant"


def is_economy_manager(
    *, is_admin: bool, role_ids: Collection[int], manager_role_id: int
) -> bool:
    """The economy-manager rule, on plain ids — admin, or the manager role.

    ``quest_views.can_manage_economy`` is the discord.Member-shaped wrapper
    over this; the id form exists because the on_message faucet resolves the
    member on the event loop but only learns ``manager_role_id`` once the
    settings load in the DB thread.
    """
    if is_admin:
        return True
    return manager_role_id != 0 and manager_role_id in set(role_ids)


QOTD_QUESTION_MAX = 300


def qotd_marker_question(
    *,
    content: str,
    role_mention_ids: Collection[int],
    qotd_role_id: int,
    author_is_manager: bool,
) -> str | None:
    """The question text when this message marks a new QOTD, else ``None``.

    A message becomes the day's question by **tagging the QOTD role** — the
    same role ``/qotd post`` pings, so guilds configure one dial rather than
    two. The manager gate is the security boundary: Discord lets anyone type
    ``<@&id>`` whether or not they may ping it, so without this any member
    could mint a faucet and then farm replies to it.

    An empty string is a valid result (a mod who tags the role with only an
    image still posted a question), so callers must test ``is not None``
    rather than truthiness.
    """
    if qotd_role_id <= 0 or not author_is_manager:
        return None
    if qotd_role_id not in set(role_mention_ids):
        return None
    # Drop every role/user mention — the stored question is for the audit
    # trail and the ping renders as raw "<@&123…>" noise there.
    text = re.sub(r"<@[!&]?\d+>", " ", content)
    return " ".join(text.split())[:QOTD_QUESTION_MAX]


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
