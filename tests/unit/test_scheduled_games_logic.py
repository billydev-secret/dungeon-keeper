"""Tier 1 unit tests: scheduled-games recurrence math (pure, no I/O)."""

from datetime import datetime, timedelta

from bot_modules.services.scheduled_games_service import (
    _local_to_epoch,
    compute_next_run,
)


def _epoch(y, mo, d, h, mi, offset=0.0):
    """UTC epoch for a guild-local wall-clock time at the given offset."""
    return _local_to_epoch(datetime(y, mo, d, h, mi), offset)


def _local(epoch, offset=0.0):
    return datetime(1970, 1, 1) + timedelta(seconds=epoch) + timedelta(hours=offset)


# ── once ────────────────────────────────────────────────────────────────────

def test_once_returns_exact_slot():
    # 2026-06-10 20:00 local at UTC+0
    now = _epoch(2026, 6, 8, 12, 0)
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="once",
        time_of_day_min=20 * 60, start_date="2026-06-10",
    )
    assert _local(nxt) == datetime(2026, 6, 10, 20, 0)


def test_once_in_past_is_returned_for_fire_late():
    now = _epoch(2026, 6, 8, 12, 0)
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="once",
        time_of_day_min=9 * 60, start_date="2026-06-01",
    )
    assert nxt is not None and nxt < now  # past slot → fires late


def test_once_missing_date_returns_none():
    assert compute_next_run(
        now_utc=0, offset_hours=0.0, recurrence="once",
        time_of_day_min=600, start_date=None,
    ) is None


# ── daily ───────────────────────────────────────────────────────────────────

def test_daily_today_when_slot_still_ahead():
    now = _epoch(2026, 6, 8, 10, 0)            # 10:00 local
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="daily", time_of_day_min=20 * 60,
    )
    assert _local(nxt) == datetime(2026, 6, 8, 20, 0)


def test_daily_rolls_to_tomorrow_when_slot_passed():
    now = _epoch(2026, 6, 8, 21, 0)            # 21:00, past today's 20:00
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="daily", time_of_day_min=20 * 60,
    )
    assert _local(nxt) == datetime(2026, 6, 9, 20, 0)


def test_daily_at_exact_slot_pushes_next_day():
    now = _epoch(2026, 6, 8, 20, 0)            # exactly 20:00
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="daily", time_of_day_min=20 * 60,
    )
    assert _local(nxt) == datetime(2026, 6, 9, 20, 0)


def test_daily_many_missed_collapse_to_one_then_next_future():
    # Bot down from 6/5; recovers 6/8 10:00. Stored slot was 6/5 20:00 (fires once now).
    # Advancing with after=now must jump to the next future slot, not 6/6.
    recover = _epoch(2026, 6, 8, 10, 0)
    nxt = compute_next_run(
        now_utc=recover, offset_hours=0.0, recurrence="daily",
        time_of_day_min=20 * 60, after=recover,
    )
    assert _local(nxt) == datetime(2026, 6, 8, 20, 0)


# ── weekly ──────────────────────────────────────────────────────────────────

def test_weekly_picks_next_matching_weekday():
    # 2026-06-08 is a Monday (weekday 0). Schedule Wed(2)+Fri(4) at 18:00.
    now = _epoch(2026, 6, 8, 10, 0)
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="weekly",
        time_of_day_min=18 * 60, recur_days=[2, 4],
    )
    assert _local(nxt) == datetime(2026, 6, 10, 18, 0)  # Wed


def test_weekly_same_day_but_passed_wraps_to_next_selected():
    # Monday 19:00, schedule Monday(0)+Thursday(3) at 18:00 → Monday passed → Thursday.
    now = _epoch(2026, 6, 8, 19, 0)
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="weekly",
        time_of_day_min=18 * 60, recur_days=[0, 3],
    )
    assert _local(nxt) == datetime(2026, 6, 11, 18, 0)  # Thursday


def test_weekly_single_day_wraps_a_full_week():
    # Monday 19:00, only Monday selected, 18:00 slot passed → next Monday.
    now = _epoch(2026, 6, 8, 19, 0)
    nxt = compute_next_run(
        now_utc=now, offset_hours=0.0, recurrence="weekly",
        time_of_day_min=18 * 60, recur_days=[0],
    )
    assert _local(nxt) == datetime(2026, 6, 15, 18, 0)


def test_weekly_empty_days_returns_none():
    assert compute_next_run(
        now_utc=0, offset_hours=0.0, recurrence="weekly",
        time_of_day_min=600, recur_days=[],
    ) is None


# ── timezone offset (wall-clock stability) ───────────────────────────────────

def test_fractional_offset_preserves_local_walltime():
    # UTC+5.5 (India). Daily 09:00 local must land at 03:30 UTC.
    now = _epoch(2026, 6, 8, 8, 0, offset=5.5)
    nxt = compute_next_run(
        now_utc=now, offset_hours=5.5, recurrence="daily", time_of_day_min=9 * 60,
    )
    assert _local(nxt, offset=5.5) == datetime(2026, 6, 8, 9, 0)
    # 09:00 local at +5.5 == 03:30 UTC
    assert _local(nxt, offset=0.0) == datetime(2026, 6, 8, 3, 30)


def test_negative_offset_local_walltime():
    # UTC-8 (PST). Daily 20:00 local.
    now = _epoch(2026, 6, 8, 10, 0, offset=-8.0)
    nxt = compute_next_run(
        now_utc=now, offset_hours=-8.0, recurrence="daily", time_of_day_min=20 * 60,
    )
    assert _local(nxt, offset=-8.0) == datetime(2026, 6, 8, 20, 0)
