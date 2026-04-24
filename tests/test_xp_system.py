from __future__ import annotations

import sqlite3

import pytest

from xp_system import (
    DEFAULT_XP_SETTINGS,
    XP_SOURCE_GRANT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    MessageXpContext,
    apply_xp_award,
    calculate_message_xp,
    get_member_last_activity_map,
    get_member_xp_state,
    get_oldest_xp_event_timestamp,
    get_user_xp_standing,
    get_xp_distribution_stats,
    get_xp_leaderboard,
    init_xp_tables,
    is_channel_xp_eligible,
    is_message_processed,
    level_for_xp,
    mark_message_processed,
    qualified_words,
    record_member_activity,
    record_xp_event,
    xp_required_for_level,
)


@pytest.fixture
def xp_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_xp_tables(conn)
    yield conn
    conn.close()


def test_qualified_words_filters_urls_and_junk():
    words = qualified_words("hi wow!!! https://example.com 😀 <:wave:123> alpha beta2 ...")
    assert words == ["wow", "alpha", "beta2"]


def test_calculate_message_xp_applies_all_modifiers():
    breakdown = calculate_message_xp(
        MessageXpContext(
            content="alpha beta gamma delta",
            seconds_since_last_message=5,
            is_duplicate=True,
            is_reply_to_human=True,
            pair_streak=DEFAULT_XP_SETTINGS.pair_streak_threshold,
        ),
        DEFAULT_XP_SETTINGS,
    )
    assert breakdown.qualified_words == 4
    assert breakdown.normalized_content == "alpha beta gamma delta"
    assert breakdown.awarded_xp == 0.02


def test_apply_xp_award_levels_up_and_marks_role_reward(xp_conn):
    level_5_threshold = xp_required_for_level(DEFAULT_XP_SETTINGS.role_grant_level, DEFAULT_XP_SETTINGS)
    first = apply_xp_award(xp_conn, guild_id=1, user_id=42, xp_delta=level_5_threshold - 0.01, settings=DEFAULT_XP_SETTINGS)
    second = apply_xp_award(xp_conn, guild_id=1, user_id=42, xp_delta=0.01, message_timestamp=123.0, message_norm="alpha beta", settings=DEFAULT_XP_SETTINGS)
    state = get_member_xp_state(xp_conn, guild_id=1, user_id=42)

    assert first.new_level == 4
    assert first.role_grant_due is False
    assert second.new_level == 5
    assert second.role_grant_due is True
    assert state.total_xp == level_5_threshold
    assert state.level == 5
    assert state.last_message_at == 123.0
    assert state.last_message_norm == "alpha beta"


def test_level_for_xp_uses_sqrt_thresholds():
    level_4_threshold = xp_required_for_level(4, DEFAULT_XP_SETTINGS)
    level_5_threshold = xp_required_for_level(5, DEFAULT_XP_SETTINGS)
    assert level_4_threshold == 140.4
    assert level_5_threshold == 249.6
    assert level_for_xp(level_4_threshold - 0.01, DEFAULT_XP_SETTINGS) == 3
    assert level_for_xp(level_4_threshold, DEFAULT_XP_SETTINGS) == 4
    assert level_for_xp(level_5_threshold - 0.01, DEFAULT_XP_SETTINGS) == 4
    assert level_for_xp(level_5_threshold, DEFAULT_XP_SETTINGS) == 5


def test_get_member_xp_state_recalculates_cached_level_from_total_xp(xp_conn):
    xp_conn.execute(
        "INSERT INTO member_xp (guild_id, user_id, total_xp, level, last_message_at, last_message_norm) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 42, xp_required_for_level(5, DEFAULT_XP_SETTINGS), 2, 123.0, "alpha"),
    )
    state = get_member_xp_state(xp_conn, guild_id=1, user_id=42, settings=DEFAULT_XP_SETTINGS)
    assert state.level == 5
    assert state.total_xp == xp_required_for_level(5, DEFAULT_XP_SETTINGS)
    assert state.last_message_at == 123.0
    assert state.last_message_norm == "alpha"


def test_channel_xp_is_enabled_by_default_and_blocked_when_excluded():
    assert is_channel_xp_eligible(channel_id=10, parent_id=None, excluded_channel_ids=set()) is True
    assert is_channel_xp_eligible(channel_id=10, parent_id=None, excluded_channel_ids={10}) is False
    assert is_channel_xp_eligible(channel_id=11, parent_id=10, excluded_channel_ids={10}) is False


def test_leaderboard_filters_by_source_and_time_window(xp_conn):
    apply_xp_award(xp_conn, guild_id=1, user_id=10, xp_delta=50.0, event_source=XP_SOURCE_TEXT, event_timestamp=1_000.0, settings=DEFAULT_XP_SETTINGS)
    apply_xp_award(xp_conn, guild_id=1, user_id=11, xp_delta=20.0, event_source=XP_SOURCE_TEXT, event_timestamp=2_000.0, settings=DEFAULT_XP_SETTINGS)
    apply_xp_award(xp_conn, guild_id=1, user_id=11, xp_delta=80.0, event_source=XP_SOURCE_VOICE, event_timestamp=2_000.0, settings=DEFAULT_XP_SETTINGS)
    record_xp_event(xp_conn, guild_id=1, user_id=12, source=XP_SOURCE_REPLY, amount=7.5, created_at=2_500.0)

    all_time_text = get_xp_leaderboard(xp_conn, guild_id=1, source=XP_SOURCE_TEXT, limit=5)
    recent_text = get_xp_leaderboard(xp_conn, guild_id=1, source=XP_SOURCE_TEXT, since_ts=1_500.0, limit=5)
    reply_board = get_xp_leaderboard(xp_conn, guild_id=1, source=XP_SOURCE_REPLY, limit=5)

    assert [(e.user_id, e.xp) for e in all_time_text] == [(10, 50.0), (11, 20.0)]
    assert [(e.user_id, e.xp) for e in recent_text] == [(11, 20.0)]
    assert [(e.user_id, e.xp) for e in reply_board] == [(12, 7.5)]


def test_xp_distribution_stats_report_member_count_median_and_stddev(xp_conn):
    for uid, amt, ts in [(10, 10.0, 100.0), (11, 20.0, 100.0), (12, 30.0, 100.0), (12, 10.0, 150.0)]:
        record_xp_event(xp_conn, guild_id=1, user_id=uid, source=XP_SOURCE_TEXT, amount=amt, created_at=ts)

    stats = get_xp_distribution_stats(xp_conn, guild_id=1, source=XP_SOURCE_TEXT)
    recent_stats = get_xp_distribution_stats(xp_conn, guild_id=1, source=XP_SOURCE_TEXT, since_ts=125.0)

    assert (stats.member_count, stats.median_xp, stats.stddev_xp) == (3, 20.0, 12.47)
    assert (recent_stats.member_count, recent_stats.median_xp, recent_stats.stddev_xp) == (1, 10.0, 0.0)


def test_processed_message_tracking_is_idempotent(xp_conn):
    assert is_message_processed(xp_conn, guild_id=1, message_id=123) is False
    for _ in range(2):
        mark_message_processed(xp_conn, guild_id=1, message_id=123, channel_id=10, user_id=20, created_at=1000.0)
    assert is_message_processed(xp_conn, guild_id=1, message_id=123) is True
    assert xp_conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0] == 1


def test_member_activity_keeps_latest_message_per_member(xp_conn):
    for ch, mid, ts in [(100, 1000, 200.0), (101, 1001, 150.0), (102, 1002, 300.0)]:
        record_member_activity(xp_conn, guild_id=1, user_id=10, channel_id=ch, message_id=mid, created_at=ts)
    record_member_activity(xp_conn, guild_id=1, user_id=11, channel_id=103, message_id=1003, created_at=250.0)

    activities = get_member_last_activity_map(xp_conn, guild_id=1, user_ids=[10, 11, 12])
    assert sorted(activities.keys()) == [10, 11]
    assert (activities[10].channel_id, activities[10].message_id, activities[10].created_at) == (102, 1002, 300.0)
    assert (activities[11].channel_id, activities[11].message_id, activities[11].created_at) == (103, 1003, 250.0)


def test_oldest_xp_event_timestamp_filters_by_source(xp_conn):
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_VOICE, amount=5.0, created_at=300.0)
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=200.0)
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_REPLY, amount=5.0, created_at=100.0)

    assert get_oldest_xp_event_timestamp(xp_conn, guild_id=1) == 100.0
    assert get_oldest_xp_event_timestamp(xp_conn, guild_id=1, sources=(XP_SOURCE_TEXT, XP_SOURCE_REPLY)) == 100.0
    assert get_oldest_xp_event_timestamp(xp_conn, guild_id=1, sources=(XP_SOURCE_VOICE,)) == 300.0


def test_user_xp_standing_reports_rank_and_missing_user(xp_conn):
    for uid, amt in [(10, 12.0), (11, 20.0), (12, 12.0)]:
        record_xp_event(xp_conn, guild_id=1, user_id=uid, source=XP_SOURCE_TEXT, amount=amt, created_at=100.0)

    standing = get_user_xp_standing(xp_conn, guild_id=1, source=XP_SOURCE_TEXT, user_id=12)
    missing = get_user_xp_standing(xp_conn, guild_id=1, source=XP_SOURCE_TEXT, user_id=99)
    assert (standing.rank, standing.xp) == (3, 12.0)
    assert (missing.rank, missing.xp) == (None, 0.0)


def test_manual_grant_source_records_event(xp_conn):
    award = apply_xp_award(xp_conn, guild_id=1, user_id=50, xp_delta=DEFAULT_XP_SETTINGS.manual_grant_xp, event_source=XP_SOURCE_GRANT, event_timestamp=500.0, settings=DEFAULT_XP_SETTINGS)
    board = get_xp_leaderboard(xp_conn, guild_id=1, source=XP_SOURCE_GRANT, limit=5)
    assert award.awarded_xp == DEFAULT_XP_SETTINGS.manual_grant_xp
    assert [(e.user_id, e.xp) for e in board] == [(50, DEFAULT_XP_SETTINGS.manual_grant_xp)]
