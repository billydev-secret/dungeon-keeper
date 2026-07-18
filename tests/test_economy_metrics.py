"""Tests for the Stage-4 metrics rollup — economy/metrics (pure) + the service.

Pure math: ISO-week epoch bounds and day range (year-rollover cases), median /
nearest-rank p90 over even and odd earner counts, the faucet-mix share split,
and the pricing hints (factors + zero-median short-circuit). Service: a full
seeded week rolled up end-to-end asserting every column (transfers excluded both
directions, rentals live/ended, streak/grace windows), PK idempotency, the
newest-first history read, and the latest-median helper.
"""

from __future__ import annotations

import json
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy import metrics
from bot_modules.services.economy_metrics_service import (
    compute_weekly_rollup,
    get_weekly_metrics,
    latest_median_income,
)
from bot_modules.services.economy_service import EconSettings
from migrations import apply_migrations_sync

GUILD = 700
USER = 2001
OTHER = 2002
THIRD = 2003
WEEK = "2026-W28"  # Mon 2026-07-06 .. Sun 2026-07-12 (offset 0)
OFFSET = 0.0
SETTINGS = EconSettings(enabled=True)
NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


# ── pure: ISO-week bounds / day range ──────────────────────────────────


def test_iso_week_bounds_tile_with_local_days():
    start, end = metrics.iso_week_bounds(WEEK, OFFSET)
    # A full week is seven days long.
    assert end - start == 7 * 86400
    # Monday 00:00 through the following Monday 00:00.
    from bot_modules.economy.logic import local_day_bounds

    assert start == local_day_bounds("2026-07-06", OFFSET)[0]
    assert end == local_day_bounds("2026-07-13", OFFSET)[0]


def test_iso_week_bounds_year_rollover_2026_w01():
    # 2026-W01 begins on Monday 2025-12-29 (prior calendar year).
    mon, sun = metrics.iso_week_day_range("2026-W01")
    assert mon == "2025-12-29"
    assert sun == "2026-01-04"


def test_iso_week_bounds_year_rollover_2020_w53():
    # 2020 has an ISO week 53 spanning the year boundary.
    mon, sun = metrics.iso_week_day_range("2020-W53")
    assert mon == "2020-12-28"
    assert sun == "2021-01-03"
    start, end = metrics.iso_week_bounds("2020-W53", OFFSET)
    assert end - start == 7 * 86400


def test_iso_week_bounds_honors_offset():
    # A negative offset pushes the local-midnight boundaries later in UTC.
    s0, _ = metrics.iso_week_bounds(WEEK, 0.0)
    s7, _ = metrics.iso_week_bounds(WEEK, -7.0)
    assert s7 == s0 + 7 * 3600


def test_malformed_iso_week_rejected():
    with pytest.raises(ValueError):
        metrics.iso_week_day_range("2026W28")


# ── pure: median / p90 ─────────────────────────────────────────────────


def test_median_odd_and_even_counts():
    assert metrics.median_income([10, 20, 30]) == 20.0  # odd → middle
    assert metrics.median_income([10, 20, 30, 40]) == 25.0  # even → mean of middle
    assert metrics.median_income([]) == 0.0


def test_p90_nearest_rank_odd_and_even():
    # n=10: ceil(0.9*10)=9 → 9th smallest = 9.
    assert metrics.p90_income(list(range(1, 11))) == 9.0
    # n=5: ceil(0.9*5)=5 → the max.
    assert metrics.p90_income([5, 1, 4, 2, 3]) == 5.0
    # n=1: the sole value; empty → 0.0.
    assert metrics.p90_income([42]) == 42.0
    assert metrics.p90_income([]) == 0.0


# ── pure: faucet shares ────────────────────────────────────────────────


def test_faucet_shares_group_and_sum_to_one():
    by_kind = {"login": 20, "milestone": 10, "conversion": 30, "quest": 40}
    shares = metrics.faucet_shares(by_kind, 100)
    assert shares["logins"] == 0.3  # login + milestone
    assert shares["activity"] == 0.3
    assert shares["quests"] == 0.4
    assert shares["games"] == 0.0
    assert round(sum(shares.values()), 3) == 1.0


def test_faucet_shares_empty_on_zero_minted():
    assert metrics.faucet_shares({}, 0) == {}
    assert metrics.faucet_shares({"login": 5}, 0) == {}


# ── pure: pricing hints ────────────────────────────────────────────────


def test_pricing_hints_default_ratios_at_median_100():
    hints = metrics.pricing_hints(100.0, SETTINGS)
    assert hints["price_role_color"] == 50
    assert hints["price_role_name"] == 35
    assert hints["price_role_icon"] == 75
    assert hints["price_role_gradient"] == 120
    assert hints["price_gift_color"] == 50
    assert hints["price_text_room"] == 200
    assert hints["price_voice_room"] == 200


def test_pricing_hints_scales_with_median():
    hints = metrics.pricing_hints(200.0, SETTINGS)
    assert hints["price_role_color"] == 100  # round(200 * 0.5)


def test_pricing_hints_empty_on_nonpositive_median():
    assert metrics.pricing_hints(0.0, SETTINGS) == {}
    assert metrics.pricing_hints(-5.0, SETTINGS) == {}


# ── service seeding helpers ─────────────────────────────────────────────


def _ledger(db, user, amount, kind, ts):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger "
            "(guild_id, user_id, amount, kind, actor_id, meta, created_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?)",
            (GUILD, user, amount, kind, ts),
        )


def _activity(db, user, when):
    with open_db(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO member_activity "
            "(guild_id, user_id, last_channel_id, last_message_id, last_message_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (GUILD, user, when),
        )


def _rental(db, user, perk, state, *, beneficiary=None, ended_at=None):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_rentals "
            "(guild_id, user_id, perk, state, price, started_at, next_bill_at, "
            " beneficiary_id, created_at, ended_at) "
            "VALUES (?, ?, ?, ?, 50, 0, 0, ?, 0, ?)",
            (GUILD, user, perk, state, beneficiary or user, ended_at),
        )


def _streak(db, user, streak, last_login_day, last_grace_day=None):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_streaks "
            "(guild_id, user_id, current_streak, longest_streak, "
            " last_login_day, last_grace_day) VALUES (?, ?, ?, ?, ?, ?)",
            (GUILD, user, streak, streak, last_login_day, last_grace_day),
        )


def _seed_full_week(db):
    """Seed one fully-populated closed week and return its (start, end)."""
    start, end = metrics.iso_week_bounds(WEEK, OFFSET)
    ts = start + 3600  # inside the window

    # Income (positive credits; transfer_in excluded): earners 3.
    _ledger(db, USER, 20, "login", ts)
    _ledger(db, USER, 30, "conversion", ts)  # USER income 50
    _ledger(db, OTHER, 100, "quest", ts)  # OTHER income 100
    _ledger(db, THIRD, 10, "grant", ts)  # THIRD income 10
    _ledger(db, USER, 500, "transfer_in", ts)  # excluded from income & minted

    # Burn: a rental debit counts; transfer_out is excluded.
    _ledger(db, USER, -40, "rental", ts)  # burned 40
    _ledger(db, OTHER, -70, "transfer_out", ts)  # excluded from burned

    # Activity in the last 30 days → active_members 3.
    now = time.time()
    for uid in (USER, OTHER, THIRD):
        _activity(db, uid, now)

    # Rentals: two live (distinct beneficiaries), one ended in-week, one ended
    # before the week (not counted).
    _rental(db, USER, "role_color", "active")
    _rental(db, OTHER, "role_name", "grace")
    _rental(db, THIRD, "role_icon", "lapsed", ended_at=start + 100)
    _rental(db, USER, "role_gradient", "cancelled", ended_at=start - 100)

    # Streaks: two rows >=7 with a login this week; one below threshold; one
    # grace consumed this week.
    _streak(db, USER, 10, "2026-07-10", last_grace_day="2026-07-08")
    _streak(db, OTHER, 7, "2026-07-06")
    _streak(db, THIRD, 5, "2026-07-11")
    return start, end


# ── service: full rollup ────────────────────────────────────────────────


def test_compute_weekly_rollup_every_column(db):
    _seed_full_week(db)
    with open_db(db) as conn:
        row = compute_weekly_rollup(
            conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW
        )
    assert row is not None

    assert row["iso_week"] == WEEK
    # Incomes [10, 50, 100] → median 50, p90 nearest-rank (rank 3) = 100.
    assert row["earners"] == 3
    assert row["median_income"] == 50.0
    assert row["p90_income"] == 100.0
    assert row["active_members"] == 3

    assert row["minted"] == 160  # 20 + 30 + 100 + 10 (transfer_in excluded)
    assert row["burned"] == 40  # rental debit only (transfer_out excluded)

    expected_mix = metrics.faucet_shares(
        {"login": 20, "conversion": 30, "quest": 100, "grant": 10}, 160
    )
    assert json.loads(row["faucet_mix"]) == expected_mix

    assert row["rental_holders"] == 2  # USER + OTHER live
    assert row["rentals_live"] == 2
    assert row["rentals_ended"] == 1  # only the in-week ended rental

    assert row["streaks_7plus"] == 2  # USER(10) + OTHER(7), THIRD(5) excluded
    assert row["grace_used"] == 1  # USER's grace this week
    assert row["computed_at"] == NOW


def test_compute_weekly_rollup_idempotent_returns_none(db):
    _seed_full_week(db)
    with open_db(db) as conn:
        first = compute_weekly_rollup(
            conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW
        )
        assert first is not None
        # Replay: PK collision → no recompute, returns None, still one row.
        again = compute_weekly_rollup(
            conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW + 999
        )
        assert again is None
        n = conn.execute(
            "SELECT COUNT(*) FROM econ_metrics_weekly WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
        assert n == 1
        # The original computed_at was NOT overwritten by the replay.
        stored = conn.execute(
            "SELECT computed_at FROM econ_metrics_weekly WHERE guild_id = ?",
            (GUILD,),
        ).fetchone()[0]
        assert stored == NOW


def test_empty_week_rolls_up_zeros(db):
    with open_db(db) as conn:
        row = compute_weekly_rollup(
            conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW
        )
    assert row is not None
    assert row["earners"] == 0
    assert row["median_income"] == 0.0
    assert row["p90_income"] == 0.0
    assert row["minted"] == 0
    assert row["burned"] == 0
    assert json.loads(row["faucet_mix"]) == {}  # empty mix on zero minted
    assert row["rentals_ended"] == 0
    assert row["streaks_7plus"] == 0


def test_transfers_excluded_both_directions(db):
    start, _ = metrics.iso_week_bounds(WEEK, OFFSET)
    ts = start + 3600
    _ledger(db, USER, 500, "transfer_in", ts)
    _ledger(db, USER, -500, "transfer_out", ts)
    with open_db(db) as conn:
        row = compute_weekly_rollup(
            conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW
        )
    assert row is not None
    assert row["minted"] == 0
    assert row["burned"] == 0
    assert row["earners"] == 0  # transfer_in is not income


def test_ledger_outside_window_ignored(db):
    start, end = metrics.iso_week_bounds(WEEK, OFFSET)
    _ledger(db, USER, 100, "login", start - 10)  # before the week
    _ledger(db, USER, 100, "login", end + 10)  # after the week
    with open_db(db) as conn:
        row = compute_weekly_rollup(
            conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW
        )
    assert row is not None
    assert row["minted"] == 0
    assert row["earners"] == 0


# ── service: history reads ──────────────────────────────────────────────


def test_get_weekly_metrics_newest_first(db):
    with open_db(db) as conn:
        for wk in ("2026-W26", "2026-W28", "2026-W27"):
            compute_weekly_rollup(
                conn, SETTINGS, GUILD, wk, offset_hours=OFFSET, now=NOW
            )
        rows = get_weekly_metrics(conn, GUILD, limit=12)
    assert [r["iso_week"] for r in rows] == ["2026-W28", "2026-W27", "2026-W26"]


def test_get_weekly_metrics_respects_limit(db):
    with open_db(db) as conn:
        for wk in ("2026-W25", "2026-W26", "2026-W27"):
            compute_weekly_rollup(
                conn, SETTINGS, GUILD, wk, offset_hours=OFFSET, now=NOW
            )
        rows = get_weekly_metrics(conn, GUILD, limit=2)
    assert [r["iso_week"] for r in rows] == ["2026-W27", "2026-W26"]


def test_latest_median_income_and_empty(db):
    with open_db(db) as conn:
        assert latest_median_income(conn, GUILD) == 0.0  # no rollups yet
    _seed_full_week(db)
    with open_db(db) as conn:
        compute_weekly_rollup(conn, SETTINGS, GUILD, WEEK, offset_hours=OFFSET, now=NOW)
        assert latest_median_income(conn, GUILD) == 50.0
