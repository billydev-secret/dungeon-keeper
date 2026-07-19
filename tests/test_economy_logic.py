"""Tests for bot_modules/economy/logic.py — pure faucet math.

The streak/grace evaluator is the subtlest logic in the stage, so it gets
table-driven single-step cases plus multi-day sequence replays (miss/login/miss,
grace window recovery). Conversion and payout amounts are covered alongside.
"""

from __future__ import annotations

import pytest

from bot_modules.economy.logic import (
    LoginEval,
    convert_xp,
    evaluate_login,
    local_day_bounds,
    local_day_for,
    login_amount,
    milestone_amount,
)
from bot_modules.services.economy_service import EconSettings

SETTINGS = EconSettings()


# ── local day math ────────────────────────────────────────────────────


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    from datetime import datetime, timezone

    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_local_day_for_utc():
    assert local_day_for(_utc(2026, 7, 10, 0, 30), 0.0) == "2026-07-10"


def test_local_day_for_negative_offset_shifts_back():
    # 00:30 UTC is still the previous day at UTC-7.
    assert local_day_for(_utc(2026, 7, 10, 0, 30), -7.0) == "2026-07-09"


def test_local_day_for_fractional_offset():
    # 23:45 UTC at +0.5 crosses into the next day.
    ts = _utc(2026, 7, 10, 23, 45)
    assert local_day_for(ts, 0.5) == "2026-07-11"
    assert local_day_for(ts, 0.0) == "2026-07-10"


def test_local_day_bounds_utc():
    start, end = local_day_bounds("2026-07-10", 0.0)
    assert end - start == 86400.0
    assert local_day_for(start, 0.0) == "2026-07-10"
    assert local_day_for(end - 1, 0.0) == "2026-07-10"
    assert local_day_for(end, 0.0) == "2026-07-11"


def test_local_day_bounds_offset_roundtrip():
    start, end = local_day_bounds("2026-07-10", -7.0)
    assert local_day_for(start, -7.0) == "2026-07-10"
    assert local_day_for(start - 1, -7.0) == "2026-07-09"
    assert local_day_for(end - 1, -7.0) == "2026-07-10"


# ── evaluate_login: single-step table ─────────────────────────────────


@pytest.mark.parametrize(
    ("today", "last_login", "streak", "last_grace", "expected"),
    [
        # First-ever login.
        (
            "2026-07-10", None, 0, None,
            LoginEval(1, grace_consumed=False, reset=False, grace_covers_day=None),
        ),
        # Consecutive day extends the streak.
        (
            "2026-07-10", "2026-07-09", 3, None,
            LoginEval(4, grace_consumed=False, reset=False, grace_covers_day=None),
        ),
        # Single missed day, grace never used -> bridged silently.
        (
            "2026-07-10", "2026-07-08", 5, None,
            LoginEval(
                6, grace_consumed=True, reset=False, grace_covers_day="2026-07-09"
            ),
        ),
        # Single missed day, grace used long ago (>= 7 days before the miss).
        (
            "2026-07-10", "2026-07-08", 5, "2026-07-02",
            LoginEval(
                6, grace_consumed=True, reset=False, grace_covers_day="2026-07-09"
            ),
        ),
        # Single missed day but grace used inside the rolling window -> reset.
        (
            "2026-07-10", "2026-07-08", 5, "2026-07-05",
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Grace used exactly 6 days before the missed day -> still inside window.
        (
            "2026-07-10", "2026-07-08", 5, "2026-07-03",
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Two missed days -> reset regardless of grace.
        (
            "2026-07-10", "2026-07-07", 9, None,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Long gap -> reset.
        (
            "2026-07-10", "2026-05-01", 40, None,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Defensive same-day call -> no change, no grace, no reset.
        (
            "2026-07-10", "2026-07-10", 4, None,
            LoginEval(4, grace_consumed=False, reset=False, grace_covers_day=None),
        ),
    ],
)
def test_evaluate_login_table(today, last_login, streak, last_grace, expected):
    result = evaluate_login(
        today=today,
        last_login_day=last_login,
        current_streak=streak,
        last_grace_day=last_grace,
    )
    assert result == expected


# ── evaluate_login: multi-day sequences ───────────────────────────────


def _replay(days: list[str]) -> tuple[LoginEval, list[int]]:
    """Replay a sequence of login days through the evaluator, carrying state."""
    last_login: str | None = None
    last_grace: str | None = None
    streak = 0
    streaks: list[int] = []
    result = LoginEval(0, grace_consumed=False, reset=False, grace_covers_day=None)
    for day in days:
        result = evaluate_login(
            today=day,
            last_login_day=last_login,
            current_streak=streak,
            last_grace_day=last_grace,
        )
        streak = result.new_streak
        last_login = day
        if result.grace_consumed:
            last_grace = result.grace_covers_day
        streaks.append(streak)
    return result, streaks


def test_sequence_consecutive_week():
    _, streaks = _replay([f"2026-07-{d:02d}" for d in range(1, 8)])
    assert streaks == [1, 2, 3, 4, 5, 6, 7]


def test_sequence_miss_login_miss_resets():
    # Login 1st–2nd, miss 3rd (grace), login 4th, miss 5th, login 6th -> reset.
    final, streaks = _replay(
        ["2026-07-01", "2026-07-02", "2026-07-04", "2026-07-06"]
    )
    assert streaks == [1, 2, 3, 1]
    assert final.reset is True
    assert final.grace_consumed is False


def test_sequence_grace_recovers_after_seven_days():
    # Grace covers 07-03; the next single miss (07-11) is 8 days later -> new grace.
    final, streaks = _replay(
        [
            "2026-07-01", "2026-07-02",           # streak 1, 2
            "2026-07-04",                         # miss 07-03 -> grace, streak 3
            "2026-07-05", "2026-07-06", "2026-07-07",
            "2026-07-08", "2026-07-09", "2026-07-10",  # streak 9
            "2026-07-12",                         # miss 07-11 -> grace again
        ]
    )
    assert streaks == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert final.grace_consumed is True
    assert final.grace_covers_day == "2026-07-11"


def test_sequence_second_miss_inside_window_resets():
    # Grace covers 07-03; a second single miss on 07-06 is inside 7 days -> reset.
    final, streaks = _replay(
        ["2026-07-01", "2026-07-02", "2026-07-04", "2026-07-05", "2026-07-07"]
    )
    assert streaks == [1, 2, 3, 4, 1]
    assert final.reset is True


def test_sequence_reset_then_rebuild():
    final, streaks = _replay(
        ["2026-07-01", "2026-07-05", "2026-07-06"]
    )
    assert streaks == [1, 1, 2]
    assert final.reset is False


# ── login_amount ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("streak", "base", "cap", "expected"),
    [
        (1, 5, 10, 5),        # no bonus on day 1
        (2, 5, 10, 6),
        (11, 5, 10, 15),      # exactly at cap
        (12, 5, 10, 15),      # cap holds; streak counter keeps growing
        (100, 5, 10, 15),
        (1, 15, 10, 15),      # voice base
        (50, 15, 10, 25),
        (5, 5, 0, 5),         # zero cap -> base only
        (3, 5, -1, 5),        # negative cap clamped
        (0, 5, 10, 5),        # defensive: streak below 1 pays base
    ],
)
def test_login_amount(streak, base, cap, expected):
    assert login_amount(streak, base, cap) == expected


# ── milestone_amount ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("streak", "expected"),
    [
        (1, 0),
        (6, 0),
        (7, 25),      # exactly on day 7
        (8, 0),
        (29, 0),
        (30, 100),
        (31, 0),
        (99, 0),
        (100, 365),
        (101, 0),
        (150, 0),
        (200, 100),   # per-100 after day 100
        (300, 100),
        (250, 0),
    ],
)
def test_milestone_amount(streak, expected):
    assert milestone_amount(streak, SETTINGS) == expected


def test_milestone_amount_uses_settings_values():
    custom = EconSettings(
        milestone_day7=1, milestone_day30=2, milestone_day100=3, milestone_per_100=4
    )
    assert milestone_amount(7, custom) == 1
    assert milestone_amount(30, custom) == 2
    assert milestone_amount(100, custom) == 3
    assert milestone_amount(200, custom) == 4


# ── convert_xp ────────────────────────────────────────────────────────


def test_convert_xp_floor_division():
    coins, remainder = convert_xp(31.0, 0.0, 15.0)
    assert coins == 2
    assert remainder == pytest.approx(1.0)


def test_convert_xp_remainder_carries_in():
    coins, remainder = convert_xp(10.0, 6.0, 15.0)
    assert coins == 1
    assert remainder == pytest.approx(1.0)


def test_convert_xp_below_one_coin_all_carries():
    coins, remainder = convert_xp(7.5, 3.0, 15.0)
    assert coins == 0
    assert remainder == pytest.approx(10.5)


def test_convert_xp_exact_multiple_zero_remainder():
    coins, remainder = convert_xp(45.0, 0.0, 15.0)
    assert coins == 3
    assert remainder == pytest.approx(0.0)


def test_convert_xp_never_negative():
    coins, remainder = convert_xp(-10.0, -5.0, 15.0)
    assert coins == 0
    assert remainder == 0.0


def test_convert_xp_zero_rate_carries_everything():
    coins, remainder = convert_xp(40.0, 2.5, 0.0)
    assert coins == 0
    assert remainder == pytest.approx(42.5)


def test_convert_xp_negative_rate_carries_everything():
    coins, remainder = convert_xp(40.0, 2.5, -3.0)
    assert coins == 0
    assert remainder == pytest.approx(42.5)


def test_convert_xp_remainder_stays_below_rate_across_days():
    remainder = 0.0
    total_coins = 0
    for _ in range(30):
        coins, remainder = convert_xp(9.7, remainder, 15.0)
        total_coins += coins
        assert 0.0 <= remainder < 15.0
    # 30 days x 9.7 XP = 291 XP -> 19 coins, 6 XP carried.
    assert total_coins == 19
    assert remainder == pytest.approx(6.0)


# ── evaluate_login: streak shields (sinks round 3, stage 2) ───────────


@pytest.mark.parametrize(
    ("today", "last_login", "last_grace", "shields", "expected"),
    [
        # Single miss, grace burned inside the window, shield steps in.
        (
            "2026-07-10", "2026-07-08", "2026-07-05", 1,
            LoginEval(
                6, grace_consumed=False, reset=False, grace_covers_day=None,
                shield_consumed=True,
            ),
        ),
        # Single miss with grace available: grace first, shield kept.
        (
            "2026-07-10", "2026-07-08", None, 1,
            LoginEval(
                6, grace_consumed=True, reset=False,
                grace_covers_day="2026-07-09", shield_consumed=False,
            ),
        ),
        # Two missed days: survives only on grace AND shield together.
        (
            "2026-07-10", "2026-07-07", None, 1,
            LoginEval(
                6, grace_consumed=True, reset=False,
                grace_covers_day="2026-07-08", shield_consumed=True,
            ),
        ),
        # Two missed days, shield but no grace: reset, shield NOT consumed.
        (
            "2026-07-10", "2026-07-07", "2026-07-05", 1,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Two missed days, grace but no shield: reset (pre-shield behavior).
        (
            "2026-07-10", "2026-07-07", None, 0,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Three missed days: reset even with both covers.
        (
            "2026-07-10", "2026-07-06", None, 1,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Defensive: shields over the cap behave like exactly one.
        (
            "2026-07-10", "2026-07-06", None, 5,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Defensive: negative shields behave like zero.
        (
            "2026-07-10", "2026-07-08", "2026-07-05", -3,
            LoginEval(1, grace_consumed=False, reset=True, grace_covers_day=None),
        ),
        # Consecutive day: shield untouched, nothing consumed.
        (
            "2026-07-10", "2026-07-09", None, 1,
            LoginEval(6, grace_consumed=False, reset=False, grace_covers_day=None),
        ),
    ],
)
def test_evaluate_login_shield_table(today, last_login, last_grace, shields, expected):
    result = evaluate_login(
        today=today,
        last_login_day=last_login,
        current_streak=5,
        last_grace_day=last_grace,
        shields_held=shields,
    )
    assert result == expected


def test_shield_save_anchors_grace_window_on_covered_day():
    # A gap-3 save consumes grace on the FIRST missed day — a single miss
    # five days later is still inside the rolling window and must reset
    # (no shield left, grace anchored on 07-08).
    first = evaluate_login(
        today="2026-07-10",
        last_login_day="2026-07-07",
        current_streak=5,
        last_grace_day=None,
        shields_held=1,
    )
    assert first.grace_covers_day == "2026-07-08"
    later = evaluate_login(
        today="2026-07-13",
        last_login_day="2026-07-11",
        current_streak=7,
        last_grace_day=first.grace_covers_day,
        shields_held=0,
    )
    assert later.reset is True
