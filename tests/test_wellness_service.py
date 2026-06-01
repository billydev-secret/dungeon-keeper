"""Unit tests for bot_modules.services.wellness_service.

Mirrors the test_activity_graphs.py / test_interaction_graph.py shape: pure
helpers first, then a migrated SQLite fixture for the CRUD/state functions.
Targets the uncovered branches called out in the task brief: cap CRUD edges,
blackout edges (incl. midnight crossing), partner pairing/dissolution,
weekly summary, streak transitions, notification preference resolution,
and the multi-table opt-out garbage collection.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import wellness_service as ws
from migrations import apply_migrations_sync


# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """A migrated SQLite connection with wellness_* tables initialised."""
    path = tmp_path / "ws.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        ws.init_wellness_tables(conn)
        yield conn


# ── Pure helpers: timezone + window math ─────────────────────────────


def test_safe_zone_none_returns_utc():
    assert ws.safe_zone(None) == ZoneInfo("UTC")


def test_safe_zone_empty_string_returns_utc():
    assert ws.safe_zone("") == ZoneInfo("UTC")


def test_safe_zone_valid_returns_zone():
    assert ws.safe_zone("America/New_York") == ZoneInfo("America/New_York")


def test_safe_zone_bad_falls_back_to_utc():
    assert ws.safe_zone("Not/A/Zone") == ZoneInfo("UTC")


def test_user_now_returns_aware_datetime():
    dt = ws.user_now("UTC")
    assert dt.tzinfo is not None


def test_window_start_for_hourly_truncates_to_top_of_hour():
    now = datetime(2026, 5, 31, 14, 37, 22, tzinfo=ZoneInfo("UTC"))
    start = ws.window_start_for("hourly", now)
    assert start == datetime(2026, 5, 31, 14, 0, 0, tzinfo=ZoneInfo("UTC"))


def test_window_start_for_daily_after_reset_hour():
    now = datetime(2026, 5, 31, 5, 0, tzinfo=ZoneInfo("UTC"))
    start = ws.window_start_for("daily", now, daily_reset_hour=4)
    # 5am > 4am reset → anchor is today 4am
    assert start == datetime(2026, 5, 31, 4, 0, tzinfo=ZoneInfo("UTC"))


def test_window_start_for_daily_before_reset_hour_rolls_back():
    now = datetime(2026, 5, 31, 3, 0, tzinfo=ZoneInfo("UTC"))
    start = ws.window_start_for("daily", now, daily_reset_hour=4)
    # 3am < 4am reset → anchor is *yesterday* 4am
    assert start == datetime(2026, 5, 30, 4, 0, tzinfo=ZoneInfo("UTC"))


def test_window_start_for_weekly_rolls_back_to_monday():
    # 2026-05-31 is a Sunday → start = previous Monday at reset hour 0
    now = datetime(2026, 5, 31, 12, 0, tzinfo=ZoneInfo("UTC"))
    start = ws.window_start_for("weekly", now)
    assert start.weekday() == 0
    assert start == datetime(2026, 5, 25, 0, 0, tzinfo=ZoneInfo("UTC"))


def test_window_start_for_unknown_raises():
    now = datetime(2026, 5, 31, 12, 0, tzinfo=ZoneInfo("UTC"))
    with pytest.raises(ValueError, match="Unknown window"):
        ws.window_start_for("yearly", now)


def test_window_start_epoch_is_integer_timestamp():
    now = datetime(2026, 5, 31, 14, 30, tzinfo=ZoneInfo("UTC"))
    eps = ws.window_start_epoch("hourly", now)
    assert isinstance(eps, int)
    assert eps == int(datetime(2026, 5, 31, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp())


# ── Pure helpers: streak math ────────────────────────────────────────


@pytest.mark.parametrize(
    "days, expected",
    [
        (0, "🌱"),
        (1, "🌱"),
        (6, "🌱"),
        (7, "🌟"),
        (29, "🌟"),
        (30, "🔥"),
        (99, "🔥"),
        (100, "💪"),
        (364, "💪"),
        (365, "👑"),
        (10_000, "👑"),
    ],
)
def test_badge_for_days_covers_all_milestones(days, expected):
    assert ws.badge_for_days(days) == expected


def test_decay_streak_floor_at_1():
    assert ws.decay_streak(0) == 1
    assert ws.decay_streak(1) == 1


def test_decay_streak_small_values():
    # math.ceil(0.10 * 2) = 1 → 2 - 1 = 1
    assert ws.decay_streak(2) == 1
    # math.ceil(0.10 * 5) = 1 → 5 - 1 = 4
    assert ws.decay_streak(5) == 4


def test_decay_streak_large_values():
    # ceil(0.10 * 100) = 10 → 90
    assert ws.decay_streak(100) == 90
    # ceil(0.10 * 365) = 37 → 328
    assert ws.decay_streak(365) == 328


def test_next_milestone_zero_returns_first():
    nm = ws.next_milestone(0)
    assert nm is not None
    threshold, _ = nm
    # First milestone above 0 is 7
    assert threshold == 7


def test_next_milestone_in_middle():
    nm = ws.next_milestone(50)
    assert nm is not None
    threshold, _ = nm
    assert threshold == 100


def test_next_milestone_at_max_returns_none():
    assert ws.next_milestone(365) is None
    assert ws.next_milestone(99999) is None


# ── Blackout math (WellnessBlackout.is_active_at) ────────────────────


def _bo(
    *,
    start_minute=9 * 60,
    end_minute=17 * 60,
    days_mask=ws.ALL_DAYS_MASK,
    enabled=True,
):
    return ws.WellnessBlackout(
        id=1,
        guild_id=1,
        user_id=1,
        name="t",
        start_minute=start_minute,
        end_minute=end_minute,
        days_mask=days_mask,
        enabled=enabled,
        created_at=0.0,
    )


def test_blackout_disabled_is_never_active():
    bo = _bo(enabled=False)
    # 12:00 on any weekday — would otherwise match
    dt = datetime(2026, 6, 1, 12, 0)  # Monday
    assert bo.is_active_at(dt) is False


def test_blackout_same_day_window_inside():
    bo = _bo()  # 9-17, all days
    dt = datetime(2026, 6, 1, 12, 0)  # Mon noon
    assert bo.is_active_at(dt) is True


def test_blackout_same_day_window_at_start():
    bo = _bo()
    dt = datetime(2026, 6, 1, 9, 0)
    assert bo.is_active_at(dt) is True


def test_blackout_same_day_window_at_end_exclusive():
    bo = _bo()
    # End is exclusive
    dt = datetime(2026, 6, 1, 17, 0)
    assert bo.is_active_at(dt) is False


def test_blackout_same_day_window_day_not_in_mask():
    bo = _bo(days_mask=ws.WEEKDAY_MASK)  # Mon-Fri
    dt = datetime(2026, 5, 31, 12, 0)  # Sunday
    assert bo.is_active_at(dt) is False


def test_blackout_midnight_crossing_late_evening():
    # 23:00–07:00, Mon only
    bo = _bo(start_minute=23 * 60, end_minute=7 * 60, days_mask=ws.DAY_BIT[0])
    dt = datetime(2026, 6, 1, 23, 30)  # Mon 23:30
    assert bo.is_active_at(dt) is True


def test_blackout_midnight_crossing_early_morning_next_day():
    # 23:00–07:00, Mon only — Tue 02:00 should be active (Mon is in mask)
    bo = _bo(start_minute=23 * 60, end_minute=7 * 60, days_mask=ws.DAY_BIT[0])
    dt = datetime(2026, 6, 2, 2, 0)  # Tue 02:00
    assert bo.is_active_at(dt) is True


def test_blackout_midnight_crossing_after_end_is_inactive():
    bo = _bo(start_minute=23 * 60, end_minute=7 * 60, days_mask=ws.DAY_BIT[0])
    dt = datetime(2026, 6, 2, 8, 0)  # Tue 08:00
    assert bo.is_active_at(dt) is False


def test_blackout_midnight_crossing_other_day_inactive():
    # Mon only → Sun 23:30 should NOT trigger
    bo = _bo(start_minute=23 * 60, end_minute=7 * 60, days_mask=ws.DAY_BIT[0])
    dt = datetime(2026, 5, 31, 23, 30)  # Sunday
    assert bo.is_active_at(dt) is False


def test_blackout_includes_day_helper():
    bo = _bo(days_mask=ws.WEEKDAY_MASK)
    assert bo.includes_day(0) is True  # Mon
    assert bo.includes_day(4) is True  # Fri
    assert bo.includes_day(5) is False  # Sat
    assert bo.includes_day(6) is False  # Sun


# ── WellnessUser dataclass: is_active / is_paused ────────────────────


def test_wellness_user_opt_in_then_opt_out_flow(db_conn):
    user = ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    assert user.is_active is True
    assert user.is_paused is False
    ws.opt_out_user(db_conn, 1, 100)
    after = ws.get_wellness_user(db_conn, 1, 100)
    assert after is not None
    assert after.is_active is False


def test_wellness_user_paused_property_reflects_future(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.pause_user(db_conn, 1, 100, until=time.time() + 3600)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.is_paused is True


def test_wellness_user_paused_property_expired(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.pause_user(db_conn, 1, 100, until=time.time() - 3600)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.is_paused is False


def test_get_wellness_user_missing_returns_none(db_conn):
    assert ws.get_wellness_user(db_conn, 1, 999) is None


# ── opt_in_user: enforcement/notif fallback ──────────────────────────


def test_opt_in_user_invalid_enforcement_falls_back_to_default(db_conn):
    user = ws.opt_in_user(
        db_conn, 1, 100, timezone="UTC", enforcement_level="BOGUS"
    )
    assert user.enforcement_level == ws.DEFAULT_ENFORCEMENT


def test_opt_in_user_invalid_notif_falls_back_to_default(db_conn):
    user = ws.opt_in_user(
        db_conn, 1, 100, timezone="UTC", notifications_pref="invalid"
    )
    assert user.notifications_pref == ws.DEFAULT_NOTIFICATIONS


def test_opt_in_user_reactivates_existing_row(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.opt_out_user(db_conn, 1, 100)
    user2 = ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    # After re-opt-in, opted_out_at should be cleared
    assert user2.opted_out_at is None
    assert user2.is_active is True


# ── update_user_settings: per-field branches ─────────────────────────


def test_update_user_settings_noop_when_all_none(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    before = ws.get_wellness_user(db_conn, 1, 100)
    ws.update_user_settings(db_conn, 1, 100)
    after = ws.get_wellness_user(db_conn, 1, 100)
    assert before == after


def test_update_user_settings_timezone(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_user_settings(db_conn, 1, 100, timezone="America/New_York")
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.timezone == "America/New_York"


def test_update_user_settings_ignores_invalid_enforcement(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC", enforcement_level="gentle")
    ws.update_user_settings(db_conn, 1, 100, enforcement_level="invalid")
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.enforcement_level == "gentle"


def test_update_user_settings_ignores_invalid_notif(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC", notifications_pref="dm")
    ws.update_user_settings(db_conn, 1, 100, notifications_pref="nope")
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.notifications_pref == "dm"


def test_update_user_settings_ignores_nonpositive_slow_rate(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_user_settings(db_conn, 1, 100, slow_mode_rate_seconds=0)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.slow_mode_rate_seconds == ws.DEFAULT_SLOW_MODE_RATE_SECONDS


def test_update_user_settings_accepts_positive_slow_rate(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_user_settings(db_conn, 1, 100, slow_mode_rate_seconds=300)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.slow_mode_rate_seconds == 300


def test_update_user_settings_public_commitment_false(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_user_settings(db_conn, 1, 100, public_commitment=False)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.public_commitment is False


def test_update_user_settings_daily_reset_hour_out_of_range_ignored(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_user_settings(db_conn, 1, 100, daily_reset_hour=25)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.daily_reset_hour == 0


def test_update_user_settings_daily_reset_hour_valid(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_user_settings(db_conn, 1, 100, daily_reset_hour=6)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.daily_reset_hour == 6


# ── pause / resume / cooldown ────────────────────────────────────────


def test_resume_user_clears_paused_until(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.pause_user(db_conn, 1, 100, until=time.time() + 3600)
    ws.resume_user(db_conn, 1, 100)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.paused_until is None


def test_set_then_clear_cooldown(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.set_cooldown(db_conn, 1, 100, time.time() + 300)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.cooldown_until is not None
    ws.clear_cooldown(db_conn, 1, 100)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None and user.cooldown_until is None


def test_list_active_users_excludes_opted_out(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.opt_in_user(db_conn, 1, 200, timezone="UTC")
    ws.opt_out_user(db_conn, 1, 200)
    active = ws.list_active_users(db_conn, 1)
    ids = {u.user_id for u in active}
    assert ids == {100}


# ── gc_opted_out_users: multi-table cascade ──────────────────────────


def test_gc_opted_out_users_purges_all_related_tables(db_conn):
    # Seed user with cascade-worthy state.
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="hourly",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    ws.increment_cap_counter(db_conn, cap_id, 1234567890)
    ws.increment_cap_overage(db_conn, cap_id, 1234567890)
    bo_id = ws.add_blackout(
        db_conn, 1, 100, name="work", start_minute=9 * 60, end_minute=17 * 60, days_mask=ws.WEEKDAY_MASK
    )
    ws.mark_blackout_active(db_conn, 1, 100, bo_id)
    ws.arm_slow_mode(
        db_conn,
        1,
        100,
        triggered_by_cap_id=cap_id,
        triggered_window_start=1234567890,
        active_until_ts=time.time() + 3600,
    )
    ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    ws.increment_streak_day(db_conn, 1, 100, "2026-05-31")
    ws.create_partner_request(db_conn, 1, 100, 200)
    ws.record_away_sent(db_conn, 1, 100, 555, time.time())
    ws.insert_weekly_report(
        db_conn,
        1,
        100,
        iso_year=2026,
        iso_week=22,
        week_start="2026-05-25",
        report_json="{}",
        ai_text="ok",
    )

    # Backdate opt-out beyond retention
    ws.opt_out_user(db_conn, 1, 100)
    db_conn.execute(
        "UPDATE wellness_users SET opted_out_at = ? WHERE user_id = ?",
        (time.time() - ws.SETTINGS_RETENTION_SECONDS - 1, 100),
    )

    deleted = ws.gc_opted_out_users(db_conn)
    assert deleted == 1

    # Verify cascade purge — every wellness_* table should be free of the user
    tables = [
        "wellness_users",
        "wellness_caps",
        "wellness_blackouts",
        "wellness_blackout_active",
        "wellness_slow_mode",
        "wellness_streaks",
        "wellness_streak_history",
        "wellness_partners",
        "wellness_away_rate_limit",
        "wellness_weekly_reports",
    ]
    for t in tables:
        if t == "wellness_partners":
            row = db_conn.execute(
                f"SELECT 1 FROM {t} WHERE user_a = ? OR user_b = ?", (100, 100)
            ).fetchone()
        else:
            row = db_conn.execute(
                f"SELECT 1 FROM {t} WHERE user_id = ?", (100,)
            ).fetchone()
        assert row is None, f"row still present in {t}"


def test_gc_opted_out_users_no_eligible_rows_returns_zero(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    # User is opted in → not eligible
    assert ws.gc_opted_out_users(db_conn) == 0


# ── Config CRUD ──────────────────────────────────────────────────────


def test_upsert_wellness_config_inserts_then_updates(db_conn):
    cfg = ws.upsert_wellness_config(db_conn, 1, role_id=42)
    assert cfg.role_id == 42
    cfg2 = ws.upsert_wellness_config(db_conn, 1, channel_id=99)
    assert cfg2.role_id == 42  # preserved
    assert cfg2.channel_id == 99


def test_upsert_wellness_config_ignores_invalid_enforcement(db_conn):
    ws.upsert_wellness_config(db_conn, 1, default_enforcement="gentle")
    cfg = ws.upsert_wellness_config(db_conn, 1, default_enforcement="bogus")
    assert cfg.default_enforcement == "gentle"


def test_get_wellness_config_missing_returns_none(db_conn):
    assert ws.get_wellness_config(db_conn, 9999) is None


# ── Caps CRUD ─────────────────────────────────────────────────────────


def test_add_cap_invalid_scope_raises(db_conn):
    with pytest.raises(ValueError, match="invalid scope"):
        ws.add_cap(
            db_conn,
            1,
            100,
            label="x",
            scope="nonsense",
            scope_target_id=0,
            window="hourly",
            cap_limit=5,
        )


def test_add_cap_invalid_window_raises(db_conn):
    with pytest.raises(ValueError, match="invalid window"):
        ws.add_cap(
            db_conn,
            1,
            100,
            label="x",
            scope="global",
            scope_target_id=0,
            window="yearly",
            cap_limit=5,
        )


def test_add_cap_nonpositive_limit_raises(db_conn):
    with pytest.raises(ValueError, match="cap_limit"):
        ws.add_cap(
            db_conn,
            1,
            100,
            label="x",
            scope="global",
            scope_target_id=0,
            window="hourly",
            cap_limit=0,
        )


def test_add_cap_with_bucket_limits_persists_json(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="b",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
        bucket_limits=[5, 8, 10],
    )
    cap = ws.get_cap(db_conn, cap_id)
    assert cap is not None
    assert cap.bucket_limits == [5, 8, 10]


def test_list_and_find_cap_by_label(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="hourly",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    caps = ws.list_caps(db_conn, 1, 100)
    assert len(caps) == 1 and caps[0].id == cap_id
    assert ws.find_cap_by_label(db_conn, 1, 100, "hourly") is not None
    assert ws.find_cap_by_label(db_conn, 1, 100, "missing") is None


def test_update_cap_limit_zero_rejected(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    assert ws.update_cap_limit(db_conn, cap_id, 0) is False
    cap = ws.get_cap(db_conn, cap_id)
    assert cap is not None and cap.cap_limit == 10


def test_update_cap_limit_valid(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    assert ws.update_cap_limit(db_conn, cap_id, 25) is True
    cap = ws.get_cap(db_conn, cap_id)
    assert cap is not None and cap.cap_limit == 25


def test_update_cap_bucket_limits_clear(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
        bucket_limits=[5, 10],
    )
    assert ws.update_cap_bucket_limits(db_conn, cap_id, None) is True
    cap = ws.get_cap(db_conn, cap_id)
    assert cap is not None and cap.bucket_limits is None


def test_update_cap_bucket_limits_sets_max_as_cap_limit(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    assert ws.update_cap_bucket_limits(db_conn, cap_id, [1, 3, 7]) is True
    cap = ws.get_cap(db_conn, cap_id)
    assert cap is not None and cap.cap_limit == 7


def test_remove_cap_returns_false_for_missing(db_conn):
    assert ws.remove_cap(db_conn, 999_999) is False


def test_remove_cap_drops_counters_and_overages(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    ws.increment_cap_counter(db_conn, cap_id, 111)
    ws.increment_cap_overage(db_conn, cap_id, 111)
    assert ws.remove_cap(db_conn, cap_id) is True
    assert (
        db_conn.execute(
            "SELECT 1 FROM wellness_cap_counters WHERE cap_id=?", (cap_id,)
        ).fetchone()
        is None
    )
    assert (
        db_conn.execute(
            "SELECT 1 FROM wellness_cap_overages WHERE cap_id=?", (cap_id,)
        ).fetchone()
        is None
    )


def test_increment_cap_counter_and_overage_increase(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    assert ws.increment_cap_counter(db_conn, cap_id, 111) == 1
    assert ws.increment_cap_counter(db_conn, cap_id, 111) == 2
    assert ws.get_cap_counter(db_conn, cap_id, 111) == 2
    assert ws.get_cap_counter(db_conn, cap_id, 222) == 0
    assert ws.increment_cap_overage(db_conn, cap_id, 111) == 1
    assert ws.increment_cap_overage(db_conn, cap_id, 111) == 2


def test_increment_blackout_overage(db_conn):
    bo_id = ws.add_blackout(
        db_conn, 1, 100, name="work", start_minute=0, end_minute=60, days_mask=ws.ALL_DAYS_MASK
    )
    assert ws.increment_blackout_overage(db_conn, bo_id, 1) == 1
    assert ws.increment_blackout_overage(db_conn, bo_id, 1) == 2


def test_gc_old_cap_data_deletes_old_rows(db_conn):
    cap_id = ws.add_cap(
        db_conn,
        1,
        100,
        label="h",
        scope="global",
        scope_target_id=0,
        window="hourly",
        cap_limit=10,
    )
    old_epoch = int(time.time()) - 30 * 86400
    new_epoch = int(time.time())
    db_conn.execute(
        "INSERT INTO wellness_cap_counters VALUES (?,?,?)",
        (cap_id, old_epoch, 5),
    )
    db_conn.execute(
        "INSERT INTO wellness_cap_counters VALUES (?,?,?)",
        (cap_id, new_epoch, 7),
    )
    deleted = ws.gc_old_cap_data(db_conn)
    assert deleted >= 1
    # New row should remain
    row = db_conn.execute(
        "SELECT count FROM wellness_cap_counters WHERE cap_id=? AND window_start_epoch=?",
        (cap_id, new_epoch),
    ).fetchone()
    assert row is not None and row["count"] == 7


# ── Blackouts CRUD ───────────────────────────────────────────────────


def test_add_list_find_blackout(db_conn):
    bid = ws.add_blackout(
        db_conn, 1, 100, name="work", start_minute=540, end_minute=1020, days_mask=ws.WEEKDAY_MASK
    )
    blackouts = ws.list_blackouts(db_conn, 1, 100)
    assert len(blackouts) == 1 and blackouts[0].id == bid
    assert ws.find_blackout_by_name(db_conn, 1, 100, "work") is not None
    assert ws.find_blackout_by_name(db_conn, 1, 100, "ghost") is None


def test_toggle_and_remove_blackout(db_conn):
    bid = ws.add_blackout(
        db_conn, 1, 100, name="w", start_minute=0, end_minute=60, days_mask=ws.ALL_DAYS_MASK
    )
    assert ws.toggle_blackout(db_conn, bid, enabled=False) is True
    bo = ws.list_blackouts(db_conn, 1, 100)[0]
    assert bo.enabled is False
    assert ws.remove_blackout(db_conn, bid) is True
    assert ws.list_blackouts(db_conn, 1, 100) == []


def test_remove_blackout_missing_returns_false(db_conn):
    assert ws.remove_blackout(db_conn, 999_999) is False


def test_mark_blackout_active_idempotent(db_conn):
    bid = ws.add_blackout(
        db_conn, 1, 100, name="x", start_minute=0, end_minute=60, days_mask=ws.ALL_DAYS_MASK
    )
    assert ws.mark_blackout_active(db_conn, 1, 100, bid) is True
    # Second call should not insert again
    assert ws.mark_blackout_active(db_conn, 1, 100, bid) is False
    markers = ws.list_active_blackout_markers(db_conn, 1, 100)
    assert markers == [bid]


def test_clear_blackout_active(db_conn):
    bid = ws.add_blackout(
        db_conn, 1, 100, name="x", start_minute=0, end_minute=60, days_mask=ws.ALL_DAYS_MASK
    )
    ws.mark_blackout_active(db_conn, 1, 100, bid)
    ws.clear_blackout_active(db_conn, 1, 100, bid)
    assert ws.list_active_blackout_markers(db_conn, 1, 100) == []


def test_blackout_templates_well_formed():
    # Spot-check the constants
    for key, tpl in ws.BLACKOUT_TEMPLATES.items():
        assert {"name", "start_minute", "end_minute", "days_mask"}.issubset(tpl.keys())


# ── Slow mode ────────────────────────────────────────────────────────


def test_arm_and_lift_slow_mode(db_conn):
    until = time.time() + 600
    ws.arm_slow_mode(
        db_conn,
        1,
        100,
        triggered_by_cap_id=42,
        triggered_window_start=1000,
        active_until_ts=until,
    )
    sm = ws.get_slow_mode(db_conn, 1, 100)
    assert sm is not None and abs(sm.active_until_ts - until) < 1
    ws.lift_slow_mode(db_conn, 1, 100)
    assert ws.get_slow_mode(db_conn, 1, 100) is None


def test_arm_slow_mode_extends_but_never_shortens(db_conn):
    # First arming: 600s from now
    a = time.time() + 600
    ws.arm_slow_mode(
        db_conn, 1, 100, triggered_by_cap_id=1, triggered_window_start=1, active_until_ts=a
    )
    # Earlier expiry should be ignored
    ws.arm_slow_mode(
        db_conn,
        1,
        100,
        triggered_by_cap_id=2,
        triggered_window_start=2,
        active_until_ts=a - 300,
    )
    sm = ws.get_slow_mode(db_conn, 1, 100)
    assert sm is not None and abs(sm.active_until_ts - a) < 1


def test_update_slow_mode_last_message_and_list_expired(db_conn):
    now = time.time()
    ws.arm_slow_mode(
        db_conn,
        1,
        100,
        triggered_by_cap_id=1,
        triggered_window_start=1,
        active_until_ts=now - 1,  # already expired
    )
    ws.update_slow_mode_last_message(db_conn, 1, 100, ts=now)
    expired = ws.list_expired_slow_mode(db_conn, now=now)
    assert len(expired) == 1
    assert abs(expired[0].last_message_ts - now) < 1


# ── Streak DB transitions ────────────────────────────────────────────


def test_ensure_streak_inserts_then_returns_existing(db_conn):
    s1 = ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    s2 = ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    assert s1.user_id == 100 and s2.user_id == 100
    assert s1.current_days == 0


def test_increment_streak_day_advances_and_records_history(db_conn):
    new, badge, upgraded = ws.increment_streak_day(db_conn, 1, 100, "2026-05-30")
    assert new == 1
    assert badge == "🌱"
    # 🌱 was already set on insert → no upgrade
    assert upgraded is False
    assert ws.has_clean_day_credit(db_conn, 1, 100, "2026-05-30") is True
    assert ws.has_clean_day_credit(db_conn, 1, 100, "2026-05-31") is False


def test_increment_streak_day_milestone_upgrade(db_conn):
    # Bump streak to day 6 manually then advance to day 7 → 🌟
    ws.ensure_streak(db_conn, 1, 100, "2026-05-25")
    db_conn.execute(
        "UPDATE wellness_streaks SET current_days = 6 WHERE guild_id = 1 AND user_id = 100"
    )
    new, badge, upgraded = ws.increment_streak_day(db_conn, 1, 100, "2026-05-31")
    assert new == 7 and badge == "🌟" and upgraded is True


def test_apply_streak_violation_decays_and_records_date(db_conn):
    ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    db_conn.execute(
        "UPDATE wellness_streaks SET current_days = 50 WHERE guild_id = 1 AND user_id = 100"
    )
    old, new = ws.apply_streak_violation(db_conn, 1, 100, "2026-05-31")
    assert old == 50
    # ceil(0.10 * 50) = 5 → 45
    assert new == 45


def test_apply_streak_violation_same_day_noop(db_conn):
    ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    db_conn.execute(
        "UPDATE wellness_streaks SET current_days = 50, last_violation_date = '2026-05-31' "
        "WHERE guild_id = 1 AND user_id = 100"
    )
    old, new = ws.apply_streak_violation(db_conn, 1, 100, "2026-05-31")
    assert old == new == 50


def test_mark_badge_celebrated_and_list_uncelebrated(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    db_conn.execute(
        "UPDATE wellness_streaks SET current_badge = '🔥' WHERE guild_id = 1 AND user_id = 100"
    )
    uncelebrated = ws.list_uncelebrated_milestones(db_conn, 1)
    assert len(uncelebrated) == 1 and uncelebrated[0][0] == 100
    ws.mark_badge_celebrated(db_conn, 1, 100, "🔥")
    assert ws.list_uncelebrated_milestones(db_conn, 1) == []


def test_list_committed_users_with_streaks_sorted_by_current_days(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.opt_in_user(db_conn, 1, 200, timezone="UTC")
    ws.opt_in_user(db_conn, 1, 300, timezone="UTC")
    # 300 opts out of public commitment
    ws.update_user_settings(db_conn, 1, 300, public_commitment=False)

    ws.ensure_streak(db_conn, 1, 100, "2026-05-30")
    ws.ensure_streak(db_conn, 1, 200, "2026-05-30")
    ws.ensure_streak(db_conn, 1, 300, "2026-05-30")
    db_conn.execute(
        "UPDATE wellness_streaks SET current_days = ? WHERE user_id = ?", (5, 100)
    )
    db_conn.execute(
        "UPDATE wellness_streaks SET current_days = ? WHERE user_id = ?", (10, 200)
    )
    db_conn.execute(
        "UPDATE wellness_streaks SET current_days = ? WHERE user_id = ?", (99, 300)
    )
    rows = ws.list_committed_users_with_streaks(db_conn, 1)
    # 300 excluded (no public_commitment) → 200 first, then 100
    assert [uid for uid, _ in rows] == [200, 100]


# ── Exempt channels ──────────────────────────────────────────────────


def test_exempt_channel_add_list_check_remove(db_conn):
    ws.add_exempt_channel(db_conn, 1, 555, "test")
    assert ws.is_channel_exempt(db_conn, 1, 555) is True
    assert ws.is_channel_exempt(db_conn, 1, 999) is False
    lst = ws.list_exempt_channels(db_conn, 1)
    assert (555, "test") in lst
    assert ws.remove_exempt_channel(db_conn, 1, 555) is True
    assert ws.remove_exempt_channel(db_conn, 1, 555) is False  # already gone


def test_add_exempt_channel_upsert_replaces_label(db_conn):
    ws.add_exempt_channel(db_conn, 1, 555, "first")
    ws.add_exempt_channel(db_conn, 1, 555, "second")
    rows = ws.list_exempt_channels(db_conn, 1)
    assert (555, "second") in rows


# ── Partners ─────────────────────────────────────────────────────────


def test_create_partner_request_self_pair_rejected(db_conn):
    result = ws.create_partner_request(db_conn, 1, 100, 100)
    assert result is None


def test_create_partner_request_orders_users_consistently(db_conn):
    p1 = ws.create_partner_request(db_conn, 1, 200, 100)
    assert p1 is not None
    # user_a is always the smaller id
    assert p1.user_a == 100 and p1.user_b == 200
    # Trying to recreate (regardless of direction) yields None
    assert ws.create_partner_request(db_conn, 1, 100, 200) is None


def test_accept_partner_request_changes_status(db_conn):
    p = ws.create_partner_request(db_conn, 1, 100, 200)
    assert p is not None and p.status == "pending"
    assert ws.accept_partner_request(db_conn, p.id) is True
    after = ws.get_partnership(db_conn, p.id)
    assert after is not None and after.status == "accepted"
    # Re-accepting already-accepted request returns False
    assert ws.accept_partner_request(db_conn, p.id) is False


def test_dissolve_partnership(db_conn):
    p = ws.create_partner_request(db_conn, 1, 100, 200)
    assert p is not None
    assert ws.dissolve_partnership(db_conn, p.id) is True
    assert ws.get_partnership(db_conn, p.id) is None
    assert ws.dissolve_partnership(db_conn, p.id) is False


def test_list_partnerships_accepted_only_flag(db_conn):
    p = ws.create_partner_request(db_conn, 1, 100, 200)
    assert p is not None
    # Pending → not returned with accepted_only=True
    assert ws.list_partnerships(db_conn, 1, 100, accepted_only=True) == []
    # Pending → returned with accepted_only=False
    assert len(ws.list_partnerships(db_conn, 1, 100, accepted_only=False)) == 1
    ws.accept_partner_request(db_conn, p.id)
    assert len(ws.list_partnerships(db_conn, 1, 100, accepted_only=True)) == 1


def test_partner_other_method(db_conn):
    p = ws.create_partner_request(db_conn, 1, 100, 200)
    assert p is not None
    assert p.other(100) == 200
    assert p.other(200) == 100


def test_remove_user_partnerships_deletes_both_sides(db_conn):
    ws.create_partner_request(db_conn, 1, 100, 200)
    ws.create_partner_request(db_conn, 1, 100, 300)
    n = ws.remove_user_partnerships(db_conn, 1, 100)
    assert n == 2
    assert ws.list_partnerships(db_conn, 1, 100, accepted_only=False) == []


# ── Away rate limit + message editor ─────────────────────────────────


def test_can_send_away_first_send_allowed(db_conn):
    assert ws.can_send_away(db_conn, 1, 100, 555, now=time.time()) is True


def test_can_send_away_rate_limits_repeated(db_conn):
    now = time.time()
    ws.record_away_sent(db_conn, 1, 100, 555, now)
    assert ws.can_send_away(db_conn, 1, 100, 555, now=now + 10) is False
    # After the rate limit window passes
    assert (
        ws.can_send_away(
            db_conn, 1, 100, 555, now=now + ws.AWAY_RATE_LIMIT_SECONDS + 1
        )
        is True
    )


def test_update_away_message_message_only(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_away_message(db_conn, 1, 100, enabled=True, message="brb")
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None
    assert user.away_enabled is True
    assert user.away_message == "brb"


def test_update_away_message_toggle_without_changing_text(db_conn):
    ws.opt_in_user(db_conn, 1, 100, timezone="UTC")
    ws.update_away_message(db_conn, 1, 100, enabled=True, message="hello")
    ws.update_away_message(db_conn, 1, 100, enabled=False)
    user = ws.get_wellness_user(db_conn, 1, 100)
    assert user is not None
    assert user.away_enabled is False
    assert user.away_message == "hello"


# ── Weekly reports ───────────────────────────────────────────────────


def test_compute_weekly_summary_empty_user(db_conn):
    week_start = date(2026, 5, 25)
    summary = ws.compute_weekly_summary(db_conn, 1, 100, week_start)
    assert summary["clean_days"] == 0
    assert summary["compliance_pct"] == 0
    assert summary["current_days"] == 0
    assert summary["badge"] == "🌱"
    assert summary["is_personal_best"] is False
    assert summary["week_start"] == "2026-05-25"
    assert summary["week_end"] == "2026-05-31"


def test_compute_weekly_summary_clean_days_and_compliance(db_conn):
    week_start = date(2026, 5, 25)
    ws.ensure_streak(db_conn, 1, 100, week_start.isoformat())
    # Insert 4 clean days within the week (compliance = 57%)
    for i in range(4):
        day = (week_start + timedelta(days=i)).isoformat()
        ws.increment_streak_day(db_conn, 1, 100, day)
    summary = ws.compute_weekly_summary(db_conn, 1, 100, week_start)
    assert summary["clean_days"] == 4
    assert summary["compliance_pct"] == round(4 / 7 * 100)
    # PB should also be 4 → is_personal_best == True
    assert summary["personal_best"] == 4
    assert summary["is_personal_best"] is True


def test_compute_weekly_summary_counts_violation_in_window(db_conn):
    week_start = date(2026, 5, 25)
    ws.ensure_streak(db_conn, 1, 100, week_start.isoformat())
    db_conn.execute(
        "UPDATE wellness_streaks SET last_violation_date = ? WHERE user_id = ?",
        ((week_start + timedelta(days=2)).isoformat(), 100),
    )
    summary = ws.compute_weekly_summary(db_conn, 1, 100, week_start)
    assert summary["violation_days"] == 1


def test_compute_weekly_summary_violation_outside_window_zero(db_conn):
    week_start = date(2026, 5, 25)
    ws.ensure_streak(db_conn, 1, 100, week_start.isoformat())
    db_conn.execute(
        "UPDATE wellness_streaks SET last_violation_date = ? WHERE user_id = ?",
        ("2025-01-01", 100),
    )
    summary = ws.compute_weekly_summary(db_conn, 1, 100, week_start)
    assert summary["violation_days"] == 0


def test_insert_and_list_weekly_reports_idempotent(db_conn):
    assert (
        ws.insert_weekly_report(
            db_conn,
            1,
            100,
            iso_year=2026,
            iso_week=22,
            week_start="2026-05-25",
            report_json='{"ok":true}',
            ai_text="hi",
        )
        is True
    )
    # Duplicate INSERT OR IGNORE should yield False
    assert (
        ws.insert_weekly_report(
            db_conn,
            1,
            100,
            iso_year=2026,
            iso_week=22,
            week_start="2026-05-25",
            report_json='{"ok":true}',
            ai_text="hi",
        )
        is False
    )
    assert ws.has_weekly_report(db_conn, 1, 100, 2026, 22) is True
    rows = ws.list_weekly_reports(db_conn, 1, 100, limit=5)
    assert len(rows) == 1
