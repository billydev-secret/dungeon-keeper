"""Tests for the pure inactive-sweep candidate selection.

The auto-sweep is a destructive mass role-strip, so its who-gets-swept decision
is isolated in ``select_sweep_candidates`` and pinned here: threshold, ordering,
exclusions, and the safety cap.
"""

from __future__ import annotations

from bot_modules.inactive.logic import (
    select_sweep_candidates,
    stale_inactive_channel_id,
)

DAY = 86400.0
NOW = 1_000_000_000.0


def _sweep(last_seen, *, threshold_days=30, exclude=None, cap=25):
    return select_sweep_candidates(
        last_seen=last_seen,
        now=NOW,
        threshold_seconds=threshold_days * DAY,
        exclude_ids=exclude or set(),
        cap=cap,
    )


def test_member_idle_past_threshold_is_selected():
    candidates, overflow = _sweep({1: NOW - 40 * DAY})
    assert [c.user_id for c in candidates] == [1]
    assert overflow == 0


def test_member_active_within_threshold_is_skipped():
    candidates, overflow = _sweep({1: NOW - 10 * DAY})
    assert candidates == []
    assert overflow == 0


def test_exactly_at_threshold_is_selected():
    # idle == threshold qualifies (>=).
    candidates, _ = _sweep({1: NOW - 30 * DAY})
    assert [c.user_id for c in candidates] == [1]


def test_excluded_ids_never_selected():
    candidates, _ = _sweep({1: NOW - 40 * DAY, 2: NOW - 40 * DAY}, exclude={1})
    assert [c.user_id for c in candidates] == [2]


def test_sorted_most_idle_first():
    candidates, _ = _sweep({1: NOW - 40 * DAY, 2: NOW - 90 * DAY, 3: NOW - 50 * DAY})
    assert [c.user_id for c in candidates] == [2, 3, 1]


def test_cap_truncates_and_reports_overflow():
    last_seen = {uid: NOW - (100 - uid) * DAY for uid in range(1, 6)}  # all idle
    candidates, overflow = _sweep(last_seen, cap=2)
    assert len(candidates) == 2
    assert overflow == 3
    # The two kept are the most idle (smallest uid here has largest idle).
    assert [c.user_id for c in candidates] == [1, 2]


def test_zero_threshold_selects_nothing():
    candidates, overflow = _sweep({1: NOW - 999 * DAY}, threshold_days=0)
    assert candidates == []
    assert overflow == 0


def test_zero_cap_selects_nothing():
    candidates, overflow = _sweep({1: NOW - 40 * DAY}, cap=0)
    assert candidates == []
    assert overflow == 0


def test_idle_seconds_is_computed():
    candidates, _ = _sweep({1: NOW - 40 * DAY})
    assert candidates[0].idle_seconds == 40 * DAY
    assert candidates[0].last_seen == NOW - 40 * DAY


def test_empty_input():
    assert _sweep({}) == ([], 0)


# ── Stale inactive-channel decision (/inactive panel re-point) ───────


def test_stale_channel_returned_when_repointed():
    assert stale_inactive_channel_id("777", 888) == 777


def test_stale_channel_none_when_unchanged():
    assert stale_inactive_channel_id("888", 888) is None


def test_stale_channel_none_when_unset():
    assert stale_inactive_channel_id(None, 888) is None
    assert stale_inactive_channel_id("", 888) is None
    assert stale_inactive_channel_id("0", 888) is None


def test_stale_channel_none_when_garbage():
    assert stale_inactive_channel_id("not-an-id", 888) is None
