"""Tests for auto_delete_service scheduling / partition decisions."""

from __future__ import annotations

from datetime import timezone

from services.auto_delete_service import (
    _BULK_DELETE_MAX_AGE,
    compute_startup_scan_after,
    is_rule_due,
    partition_messages_by_age,
)


# ── is_rule_due ───────────────────────────────────────────────────────


def test_rule_never_run_is_due():
    assert is_rule_due(now_ts=1000.0, last_run_ts=0.0, interval_seconds=60) is True


def test_rule_just_ran_not_due():
    assert is_rule_due(now_ts=1000.0, last_run_ts=999.0, interval_seconds=60) is False


def test_rule_exactly_at_interval_is_due():
    # Boundary: strict `>=` means exact match fires
    assert (
        is_rule_due(now_ts=1060.0, last_run_ts=1000.0, interval_seconds=60) is True
    )


def test_rule_past_interval_is_due():
    assert (
        is_rule_due(now_ts=2000.0, last_run_ts=1000.0, interval_seconds=60) is True
    )


def test_rule_one_second_before_interval_not_due():
    assert (
        is_rule_due(now_ts=1059.0, last_run_ts=1000.0, interval_seconds=60) is False
    )


def test_rule_future_last_run_not_due():
    # Defensive: clock skew puts last_run_ts in the future
    assert (
        is_rule_due(now_ts=1000.0, last_run_ts=1500.0, interval_seconds=60) is False
    )


# ── partition_messages_by_age ─────────────────────────────────────────


def test_partition_all_recent_go_to_bulk():
    now = 10_000.0
    messages = [(101, now - 100), (102, now - 200), (103, now - 300)]
    bulk, individual = partition_messages_by_age(messages, now)
    assert bulk == [101, 102, 103]
    assert individual == []


def test_partition_all_old_go_to_individual():
    now = 10_000.0
    age = _BULK_DELETE_MAX_AGE + 1000
    messages = [(101, now - age), (102, now - age - 100)]
    bulk, individual = partition_messages_by_age(messages, now)
    assert bulk == []
    assert individual == [101, 102]


def test_partition_mix():
    now = 10_000.0
    old = now - (_BULK_DELETE_MAX_AGE + 1000)
    recent = now - 100
    messages = [(101, recent), (102, old), (103, recent), (104, old)]
    bulk, individual = partition_messages_by_age(messages, now)
    assert bulk == [101, 103]
    assert individual == [102, 104]


def test_partition_boundary_exactly_at_limit_is_individual():
    # Uses `>` comparison — at the limit exactly is NOT bulk-eligible (safer
    # because Discord's 14-day limit is close and we want a buffer).
    now = 10_000.0
    messages = [(101, now - _BULK_DELETE_MAX_AGE)]
    bulk, individual = partition_messages_by_age(messages, now)
    assert bulk == []
    assert individual == [101]


def test_partition_just_inside_limit_is_bulk():
    now = 10_000.0
    messages = [(101, now - _BULK_DELETE_MAX_AGE + 1)]
    bulk, individual = partition_messages_by_age(messages, now)
    assert bulk == [101]
    assert individual == []


def test_partition_empty_input():
    bulk, individual = partition_messages_by_age([], 10_000.0)
    assert bulk == []
    assert individual == []


def test_partition_preserves_order():
    now = 10_000.0
    # Pre-order messages in a non-sorted way to prove we preserve input order
    messages = [(999, now - 100), (1, now - 200), (500, now - 300)]
    bulk, individual = partition_messages_by_age(messages, now)
    assert bulk == [999, 1, 500]
    assert individual == []


def test_partition_respects_custom_age_limit():
    now = 10_000.0
    messages = [(101, now - 100), (102, now - 200)]
    # Only messages < 150s old go to bulk
    bulk, individual = partition_messages_by_age(messages, now, bulk_age_limit=150)
    assert bulk == [101]
    assert individual == [102]


# ── compute_startup_scan_after ────────────────────────────────────────


def test_scan_after_never_run_returns_none():
    # last_run_ts == 0 means the rule has never run; scan everything.
    assert compute_startup_scan_after(last_run_ts=0.0, max_age_seconds=86400) is None


def test_scan_after_recent_run_returns_window_lower_bound():
    last_run = 1_000_000.0
    max_age = 86400  # 1 day
    bound = compute_startup_scan_after(last_run_ts=last_run, max_age_seconds=max_age)
    assert bound is not None
    assert bound.tzinfo is timezone.utc
    # Lower bound is last_run - max_age: anything older was already swept.
    assert bound.timestamp() == last_run - max_age


def test_scan_after_returns_none_when_bound_predates_epoch():
    # Bot just started but max_age is enormous and last_run was very recent —
    # bound goes negative, fall back to full scan.
    assert compute_startup_scan_after(last_run_ts=10.0, max_age_seconds=86400) is None


def test_scan_after_bound_exactly_at_epoch_returns_none():
    # Boundary: bound_ts == 0 also means "scan everything".
    assert (
        compute_startup_scan_after(last_run_ts=86400.0, max_age_seconds=86400) is None
    )


def test_scan_after_negative_last_run_returns_none():
    # Defensive: corrupted DB returns negative last_run_ts.
    assert compute_startup_scan_after(last_run_ts=-1.0, max_age_seconds=60) is None
