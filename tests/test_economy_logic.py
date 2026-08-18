"""Tests for bot_modules/economy/logic.py — pure faucet math.

The streak/grace evaluator is the subtlest logic in the stage, so it gets
table-driven single-step cases plus multi-day sequence replays (miss/login/miss,
grace window recovery). Conversion and payout amounts are covered alongside.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.economy.logic import (
    LoginEval,
    cat_catch_payout,
    convert_xp,
    evaluate_login,
    host_bounty_amount,
    is_economy_manager,
    local_day_bounds,
    local_day_for,
    login_amount,
    milestone_amount,
    next_week_roll_epoch,
    plan_panel_merge,
    qotd_marker_question,
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


# ── host_bounty_amount ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "joiners, per, cap, expected",
    [
        (0, 5, 5, 0),      # nobody joined -> nothing (the anti-farm gate)
        (1, 5, 5, 5),
        (3, 5, 5, 15),
        (5, 5, 5, 25),     # exactly at the cap
        (9, 5, 5, 25),     # cap holds; a huge game can't dwarf other faucets
        (3, 0, 5, 0),      # zero rate -> dark
        (3, 5, 0, 0),      # zero cap -> nothing
        (-2, 5, 5, 0),     # defensive: negative joiners never credit
    ],
)
def test_host_bounty_amount(joiners, per, cap, expected):
    assert host_bounty_amount(joiners, per, cap) == expected


# ── cat_catch_payout ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base, earned_today, daily_cap, expected",
    [
        (11, 0, 0, 11),        # uncapped (the default) -> base, untouched
        (11, 900, 0, 11),      # uncapped ignores the running total entirely
        (11, 0, 150, 11),      # well under the cap
        (11, 139, 150, 11),    # exactly reaches the cap
        (11, 145, 150, 5),     # clipped to the remaining allowance
        (11, 150, 150, 0),     # cap exactly met -> nothing more today
        (11, 400, 150, 0),     # already over (booster overshoot) -> nothing
        (300, 0, 150, 150),    # a divine catch cannot exceed the cap alone
        (0, 0, 150, 0),        # defensive: no base pays nothing
        (-5, 0, 150, 0),       # defensive: negative base never credits
        (11, -20, 150, 11),    # defensive: negative earned clamped to 0
        (11, 0, -1, 11),       # defensive: negative cap treated as uncapped
    ],
)
def test_cat_catch_payout(base, earned_today, daily_cap, expected):
    assert cat_catch_payout(
        base, earned_today=earned_today, daily_cap=daily_cap
    ) == expected


def test_cat_catch_payout_converges_on_the_cap():
    """Repeated catches sum to exactly the cap, then stop paying.

    The property that matters for a rate limiter: no sequence of catches can
    carry a member past the ceiling, and the last one is clipped rather than
    dropped (a member two coins short still earns those two).
    """
    cap, earned = 150, 0
    for _ in range(200):
        earned += cat_catch_payout(11, earned_today=earned, daily_cap=cap)
    assert earned == cap


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


# ── QOTD marker ───────────────────────────────────────────────────────────────


def _marker(**kw):
    args = {
        "content": "<@&77> what's your comfort food?",
        "role_mention_ids": [77],
        "qotd_role_id": 77,
        "author_is_manager": True,
    }
    args.update(kw)
    return qotd_marker_question(**args)


def test_qotd_marker_strips_the_ping_from_the_question():
    assert _marker() == "what's your comfort food?"


def test_qotd_marker_strips_user_mentions_and_collapses_whitespace():
    assert (
        _marker(content="<@&77>  hey  <@123> \n what's up?") == "hey what's up?"
    )


def test_qotd_marker_allows_an_empty_question():
    # A mod tagging the role with only an image still opened a question, so
    # this is "" (falsy) rather than None — callers test `is not None`.
    assert _marker(content="<@&77>") == ""


def test_qotd_marker_truncates_a_wall_of_text():
    long = _marker(content="<@&77> " + "x" * 500)
    assert len(long) == 300


def test_qotd_marker_requires_the_tag():
    assert _marker(content="just chatting", role_mention_ids=[]) is None
    assert _marker(role_mention_ids=[99]) is None


def test_qotd_marker_requires_a_manager():
    assert _marker(author_is_manager=False) is None


def test_qotd_marker_off_when_no_role_configured():
    assert _marker(qotd_role_id=0) is None


# ── manager gate ──────────────────────────────────────────────────────────────


def test_is_economy_manager_admin_always_passes():
    assert is_economy_manager(is_admin=True, role_ids=[], manager_role_id=0) is True


def test_is_economy_manager_role_holder_passes():
    assert is_economy_manager(is_admin=False, role_ids=[5, 7], manager_role_id=7) is True


def test_is_economy_manager_rejects_plain_member():
    assert is_economy_manager(is_admin=False, role_ids=[5], manager_role_id=7) is False


def test_is_economy_manager_unconfigured_role_never_matches():
    # manager_role_id 0 must not match a member who somehow holds role 0.
    assert is_economy_manager(is_admin=False, role_ids=[0], manager_role_id=0) is False


@pytest.mark.parametrize(
    "when, offset, expected",
    [
        # Tuesday midday — the roll is next Monday.
        pytest.param("2026-07-28 12:00:00", 0.0, "2026-08-03 00:00:00",
                     id="midweek"),
        # Sunday night, half an hour to go: the last-call case.
        pytest.param("2026-08-02 23:30:00", 0.0, "2026-08-03 00:00:00",
                     id="sunday-night"),
        # Exactly on the boundary the week has already closed, so the
        # deadline is the *following* Monday — otherwise the sweep would
        # announce a last call for a raffle that just drew.
        pytest.param("2026-08-03 00:00:00", 0.0, "2026-08-10 00:00:00",
                     id="exactly-on-the-roll"),
        # A guild five hours behind UTC rolls five hours later.
        pytest.param("2026-07-28 12:00:00", -5.0, "2026-08-03 05:00:00",
                     id="negative-offset"),
        pytest.param("2026-07-28 12:00:00", 5.5, "2026-08-02 18:30:00",
                     id="half-hour-offset"),
    ],
)
def test_next_week_roll_epoch(when, offset, expected):
    """The raffle's deadline is derived, not stored — guild-local Monday."""
    now = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).timestamp()
    want = datetime.fromisoformat(expected).replace(tzinfo=timezone.utc)
    assert next_week_roll_epoch(now, offset) == want.timestamp()


# ── the guide/leaderboard changeover (2026-08-18) ────────────────────────────
#
# The three ids-in-prod shapes are the three cases here, named for the guild
# that had them on changeover day. Getting this wrong deletes a live panel
# rather than a retired one, which is why the decision is pure and tested
# before it ever meets Discord.


def test_merge_deletes_the_board_and_keeps_the_guides_message():
    """Both panels posted, different channels: the board message goes."""
    plan = plan_panel_merge(
        panel_channel_id=1526017396094144584,   # 🏦│how-it-works
        panel_message_id=1528528402892722272,
        board_channel_id=1526435600654270495,   # 📈│stats
        board_message_id=1539051825519661108,
    )
    assert plan.delete == (1526435600654270495, 1539051825519661108)
    assert plan.adopt is None
    assert plan.clear is True


def test_merge_deletes_the_right_message_when_both_share_a_channel():
    """One channel holding both panels: only the *board* id may be deleted.

    The shape that makes the equal-ids guard worth having — here the two
    messages differ by one digit and sit in the same channel, so a plan that
    reached for the wrong one would look entirely plausible.
    """
    plan = plan_panel_merge(
        panel_channel_id=1532304393313980446,
        panel_message_id=1537356573813379085,
        board_channel_id=1532304393313980446,
        board_message_id=1537356574170026074,
    )
    assert plan.delete == (1532304393313980446, 1537356574170026074)
    assert plan.adopt is None


def test_merge_is_a_noop_for_a_guild_that_never_posted_a_board():
    """Guide configured, leaderboard never set up: nothing to do at all."""
    plan = plan_panel_merge(
        panel_channel_id=1529684885499936778,
        panel_message_id=1529692443644137725,
        board_channel_id=0,
        board_message_id=0,
    )
    assert plan.is_noop


def test_merge_adopts_a_board_that_has_no_guide_to_merge_into():
    """Board posted, no guide: the board's message becomes the panel.

    Not a prod shape, but orphaning a live panel — leaving a message the code
    no longer edits — is worse than keeping it where it stands.
    """
    plan = plan_panel_merge(
        panel_channel_id=0,
        panel_message_id=0,
        board_channel_id=555,
        board_message_id=666,
    )
    assert plan.adopt == (555, 666)
    assert plan.delete is None
    assert plan.clear is True


def test_merge_never_deletes_the_message_it_just_kept():
    """Both pairs naming one message: keep it, retire the duplicate record."""
    plan = plan_panel_merge(
        panel_channel_id=10, panel_message_id=20,
        board_channel_id=10, board_message_id=20,
    )
    assert plan.delete is None
    assert plan.adopt is None
    assert plan.clear is True


def test_merge_clears_a_board_channel_left_without_a_message():
    """A channel id with no message is bookkeeping; clear it, delete nothing."""
    plan = plan_panel_merge(
        panel_channel_id=10, panel_message_id=20,
        board_channel_id=99, board_message_id=0,
    )
    assert plan.delete is None
    assert plan.clear is True


def test_merge_clears_an_unreachable_board_message():
    """A message id with no channel cannot be deleted — but must still clear.

    Otherwise the one-shot plans the same impossible delete on every boot.
    """
    plan = plan_panel_merge(
        panel_channel_id=10, panel_message_id=20,
        board_channel_id=0, board_message_id=777,
    )
    assert plan.delete is None
    assert plan.clear is True


def test_merge_is_idempotent():
    """Applying the plan zeroes the board pair, so a second run plans nothing."""
    first = plan_panel_merge(
        panel_channel_id=1, panel_message_id=2,
        board_channel_id=3, board_message_id=4,
    )
    assert not first.is_noop
    second = plan_panel_merge(
        panel_channel_id=1, panel_message_id=2,
        board_channel_id=0, board_message_id=0,
    )
    assert second.is_noop
