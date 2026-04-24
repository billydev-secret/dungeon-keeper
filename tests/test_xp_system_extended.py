from __future__ import annotations

import sqlite3

import pytest

from xp_system import (
    DEFAULT_XP_SETTINGS,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    VoiceSession,
    apply_xp_award,
    completed_voice_intervals,
    cooldown_multiplier,
    count_xp_events,
    delete_voice_session,
    get_voice_session,
    has_any_member_xp,
    has_any_xp_events,
    init_xp_tables,
    list_voice_sessions,
    normalize_message_content,
    pair_multiplier,
    record_xp_event,
    role_grant_due,
    set_voice_session,
    update_pair_state,
)


@pytest.fixture
def xp_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_xp_tables(conn)
    yield conn
    conn.close()


# ── normalize_message_content ─────────────────────────────────────────

def test_lowercases_and_strips_punctuation():
    assert normalize_message_content("Hello, World!") == "hello world"


def test_removes_urls():
    assert normalize_message_content("check https://example.com out") == "check out"


def test_empty_string():
    assert normalize_message_content("") == ""


def test_only_junk_falls_back_to_stripped_lower():
    assert normalize_message_content("!!! ??? ...") == "!!! ??? ..."


def test_numbers_are_kept():
    assert "hello2" in normalize_message_content("hello2 world")


# ── cooldown_multiplier ───────────────────────────────────────────────

def test_none_returns_full_xp():
    assert cooldown_multiplier(None) == 1.0


def test_very_fast_message_is_penalized():
    assert cooldown_multiplier(1.0, DEFAULT_XP_SETTINGS) < 1.0


def test_message_after_long_gap_returns_full_xp():
    long_gap = max(DEFAULT_XP_SETTINGS.cooldown_thresholds_seconds) + 1
    assert cooldown_multiplier(long_gap) == 1.0


def test_thresholds_are_monotonically_ordered():
    thresholds = DEFAULT_XP_SETTINGS.cooldown_thresholds_seconds
    multipliers = [cooldown_multiplier(t - 0.1) for t in thresholds]
    for i in range(len(multipliers) - 1):
        assert multipliers[i] <= multipliers[i + 1]


# ── pair_multiplier ───────────────────────────────────────────────────

def test_below_threshold_returns_one():
    assert pair_multiplier(DEFAULT_XP_SETTINGS.pair_streak_threshold - 1) == 1.0


def test_at_threshold_applies_multiplier():
    assert pair_multiplier(DEFAULT_XP_SETTINGS.pair_streak_threshold) == DEFAULT_XP_SETTINGS.pair_streak_multiplier


def test_above_threshold_applies_multiplier():
    assert pair_multiplier(DEFAULT_XP_SETTINGS.pair_streak_threshold + 5) == DEFAULT_XP_SETTINGS.pair_streak_multiplier


def test_zero_streak_returns_one():
    assert pair_multiplier(0) == 1.0


# ── role_grant_due ────────────────────────────────────────────────────

def test_crossing_threshold_is_due():
    t = DEFAULT_XP_SETTINGS.role_grant_level
    assert role_grant_due(t - 1, t) is True


def test_already_above_threshold_is_not_due():
    t = DEFAULT_XP_SETTINGS.role_grant_level
    assert role_grant_due(t, t + 1) is False


def test_both_below_threshold_is_not_due():
    t = DEFAULT_XP_SETTINGS.role_grant_level
    assert role_grant_due(t - 2, t - 1) is False


def test_no_level_change_is_not_due():
    t = DEFAULT_XP_SETTINGS.role_grant_level
    assert role_grant_due(t, t) is False


# ── update_pair_state ─────────────────────────────────────────────────

def test_first_message_returns_zero_streak():
    new_state, streak = update_pair_state(None, author_id=1)
    assert streak == 0
    assert new_state.last_author_id == 1


def test_same_author_twice_resets_streak():
    state, _ = update_pair_state(None, author_id=1)
    _, streak = update_pair_state(state, author_id=1)
    assert streak == 0


def test_alternating_authors_builds_streak():
    state, _ = update_pair_state(None, author_id=1)
    state, s1 = update_pair_state(state, author_id=2)
    state, s2 = update_pair_state(state, author_id=1)
    state, s3 = update_pair_state(state, author_id=2)
    assert s1 == 1
    assert s2 == 2
    assert s3 == 3


def test_new_third_party_resets_streak():
    state, _ = update_pair_state(None, author_id=1)
    state, _ = update_pair_state(state, author_id=2)
    state, _ = update_pair_state(state, author_id=1)
    _, streak = update_pair_state(state, author_id=3)
    assert streak == 1


# ── completed_voice_intervals ─────────────────────────────────────────

_INTERVAL = DEFAULT_XP_SETTINGS.voice_interval_seconds


def _session(qualified_since, awarded_intervals=0):
    return VoiceSession(
        guild_id=1, user_id=1, channel_id=10,
        session_started_at=qualified_since or 0.0,
        qualified_since=qualified_since,
        awarded_intervals=awarded_intervals,
    )


def test_not_qualified_returns_zero():
    assert completed_voice_intervals(_session(None), now_ts=9999.0) == 0


def test_not_enough_time_returns_zero():
    assert completed_voice_intervals(_session(1000.0), now_ts=1000.0 + _INTERVAL - 1) == 0


def test_exactly_one_interval_returns_one():
    assert completed_voice_intervals(_session(1000.0), now_ts=1000.0 + _INTERVAL) == 1


def test_already_awarded_intervals_are_subtracted():
    assert completed_voice_intervals(_session(1000.0, awarded_intervals=2), now_ts=1000.0 + _INTERVAL * 5) == 3


def test_all_intervals_already_awarded_returns_zero():
    assert completed_voice_intervals(_session(1000.0, awarded_intervals=5), now_ts=1000.0 + _INTERVAL * 5) == 0


# ── VoiceSession DB ───────────────────────────────────────────────────

def test_get_missing_session_returns_none(xp_conn):
    assert get_voice_session(xp_conn, guild_id=1, user_id=99) is None


def test_set_and_get_session(xp_conn):
    set_voice_session(xp_conn, 1, 10, 100, session_started_at=500.0, qualified_since=600.0, awarded_intervals=3)
    session = get_voice_session(xp_conn, guild_id=1, user_id=10)
    assert session is not None
    assert session.channel_id == 100
    assert session.session_started_at == 500.0
    assert session.qualified_since == 600.0
    assert session.awarded_intervals == 3


def test_set_session_upserts(xp_conn):
    set_voice_session(xp_conn, 1, 10, 100, session_started_at=500.0)
    set_voice_session(xp_conn, 1, 10, 200, session_started_at=999.0, awarded_intervals=7)
    session = get_voice_session(xp_conn, guild_id=1, user_id=10)
    assert session is not None
    assert session.channel_id == 200
    assert session.awarded_intervals == 7


def test_qualified_since_can_be_none(xp_conn):
    set_voice_session(xp_conn, 1, 10, 100, session_started_at=500.0, qualified_since=None)
    session = get_voice_session(xp_conn, guild_id=1, user_id=10)
    assert session is not None
    assert session.qualified_since is None


def test_delete_session(xp_conn):
    set_voice_session(xp_conn, 1, 10, 100, session_started_at=500.0)
    delete_voice_session(xp_conn, guild_id=1, user_id=10)
    assert get_voice_session(xp_conn, guild_id=1, user_id=10) is None


def test_list_voice_sessions(xp_conn):
    set_voice_session(xp_conn, 1, 10, 100, session_started_at=500.0)
    set_voice_session(xp_conn, 1, 20, 100, session_started_at=600.0)
    set_voice_session(xp_conn, 2, 10, 200, session_started_at=700.0)
    sessions = list_voice_sessions(xp_conn)
    assert len(sessions) == 3
    assert {s.user_id for s in sessions} == {10, 20}


def test_list_voice_sessions_empty(xp_conn):
    assert list_voice_sessions(xp_conn) == []


# ── XP aggregate queries ──────────────────────────────────────────────

def test_has_any_xp_events_false_when_empty(xp_conn):
    assert has_any_xp_events(xp_conn, guild_id=1) is False


def test_has_any_xp_events_true_after_event(xp_conn):
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=100.0)
    assert has_any_xp_events(xp_conn, guild_id=1) is True


def test_has_any_xp_events_scoped_to_guild(xp_conn):
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=100.0)
    assert has_any_xp_events(xp_conn, guild_id=2) is False


def test_has_any_member_xp_false_when_empty(xp_conn):
    assert has_any_member_xp(xp_conn, guild_id=1) is False


def test_has_any_member_xp_true_after_award(xp_conn):
    apply_xp_award(xp_conn, guild_id=1, user_id=1, xp_delta=10.0, settings=DEFAULT_XP_SETTINGS)
    assert has_any_member_xp(xp_conn, guild_id=1) is True


def test_has_any_member_xp_scoped_to_guild(xp_conn):
    apply_xp_award(xp_conn, guild_id=1, user_id=1, xp_delta=10.0, settings=DEFAULT_XP_SETTINGS)
    assert has_any_member_xp(xp_conn, guild_id=2) is False


def test_count_xp_events_empty(xp_conn):
    assert count_xp_events(xp_conn, guild_id=1) == 0


def test_count_xp_events_counts_all_sources(xp_conn):
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=100.0)
    record_xp_event(xp_conn, guild_id=1, user_id=1, source=XP_SOURCE_VOICE, amount=3.0, created_at=200.0)
    record_xp_event(xp_conn, guild_id=2, user_id=1, source=XP_SOURCE_TEXT, amount=1.0, created_at=300.0)
    assert count_xp_events(xp_conn, guild_id=1) == 2
    assert count_xp_events(xp_conn, guild_id=2) == 1
