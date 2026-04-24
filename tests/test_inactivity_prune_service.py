"""Tests for services/inactivity_prune_service.compute_prune_targets."""

from __future__ import annotations

from dataclasses import dataclass

from services.inactivity_prune_service import compute_prune_targets


@dataclass
class FakeActivity:
    created_at: float


CUTOFF = 1000.0  # ts; anything before this is "inactive"


# ── empty / no-op cases ───────────────────────────────────────────────


def test_empty_roster_returns_empty():
    assert compute_prune_targets([], set(), {}, CUTOFF) == []


def test_no_activity_records_prunes_nobody():
    # User in the role but no activity record — skipped, not pruned.
    result = compute_prune_targets([(1001, False)], set(), {}, CUTOFF)
    assert result == []


# ── activity window ───────────────────────────────────────────────────


def test_activity_before_cutoff_is_pruned():
    activity = {1001: FakeActivity(created_at=CUTOFF - 1)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == [1001]


def test_activity_after_cutoff_is_kept():
    activity = {1001: FakeActivity(created_at=CUTOFF + 1)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == []


def test_activity_exactly_at_cutoff_is_kept():
    # Boundary: strict `<` means exact match is kept
    activity = {1001: FakeActivity(created_at=CUTOFF)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == []


# ── bot / exception filters ───────────────────────────────────────────


def test_bots_never_pruned():
    # Bot with ancient activity — still excluded
    activity = {1001: FakeActivity(created_at=0.0)}
    result = compute_prune_targets([(1001, True)], set(), activity, CUTOFF)
    assert result == []


def test_exempted_user_never_pruned():
    activity = {1001: FakeActivity(created_at=0.0)}
    result = compute_prune_targets([(1001, False)], {1001}, activity, CUTOFF)
    assert result == []


def test_exception_wins_over_bot_flag():
    # Both a bot AND exempted — still not pruned
    activity = {1001: FakeActivity(created_at=0.0)}
    result = compute_prune_targets([(1001, True)], {1001}, activity, CUTOFF)
    assert result == []


# ── mixed roster ──────────────────────────────────────────────────────


def test_mixed_roster_selects_correctly():
    roster = [
        (1001, False),  # inactive human → prune
        (1002, False),  # active human → keep
        (1003, True),   # inactive bot → keep (bot)
        (1004, False),  # inactive, exempted → keep
        (1005, False),  # no activity → keep
    ]
    activity = {
        1001: FakeActivity(created_at=CUTOFF - 100),
        1002: FakeActivity(created_at=CUTOFF + 100),
        1003: FakeActivity(created_at=CUTOFF - 100),
        1004: FakeActivity(created_at=CUTOFF - 100),
    }
    exceptions = {1004}
    result = compute_prune_targets(roster, exceptions, activity, CUTOFF)
    assert result == [1001]


def test_preserves_roster_order():
    roster = [(1003, False), (1001, False), (1002, False)]
    activity = {
        1001: FakeActivity(created_at=CUTOFF - 1),
        1002: FakeActivity(created_at=CUTOFF - 1),
        1003: FakeActivity(created_at=CUTOFF - 1),
    }
    result = compute_prune_targets(roster, set(), activity, CUTOFF)
    assert result == [1003, 1001, 1002]


# ── regression guards ─────────────────────────────────────────────────


def test_activity_record_for_non_roster_user_ignored():
    # activity_map may contain users not in the role; they should be ignored.
    activity = {
        1001: FakeActivity(created_at=CUTOFF - 1),
        9999: FakeActivity(created_at=CUTOFF - 1),  # not in role
    }
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == [1001]


def test_returns_list_not_set():
    # Callers may rely on list semantics (order, duplicates impossible via filter)
    activity = {1001: FakeActivity(created_at=CUTOFF - 1)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert isinstance(result, list)
