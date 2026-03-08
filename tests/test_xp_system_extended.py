import sqlite3
import unittest

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


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_xp_tables(conn)
    return conn


class NormalizeMessageContentTests(unittest.TestCase):
    def test_lowercases_and_strips_punctuation(self):
        self.assertEqual(normalize_message_content("Hello, World!"), "hello world")

    def test_removes_urls(self):
        self.assertEqual(normalize_message_content("check https://example.com out"), "check out")

    def test_empty_string(self):
        self.assertEqual(normalize_message_content(""), "")

    def test_only_junk_falls_back_to_stripped_lower(self):
        # No qualified words — falls back to URL-stripped, collapsed text
        result = normalize_message_content("!!! ??? ...")
        self.assertEqual(result, "!!! ??? ...")

    def test_numbers_are_kept(self):
        self.assertIn("hello2", normalize_message_content("hello2 world"))


class CooldownMultiplierTests(unittest.TestCase):
    def test_none_returns_full_xp(self):
        self.assertEqual(cooldown_multiplier(None), 1.0)

    def test_very_fast_message_is_penalized(self):
        result = cooldown_multiplier(1.0, DEFAULT_XP_SETTINGS)
        self.assertLess(result, 1.0)

    def test_message_after_long_gap_returns_full_xp(self):
        # Past all cooldown thresholds
        long_gap = max(DEFAULT_XP_SETTINGS.cooldown_thresholds_seconds) + 1
        self.assertEqual(cooldown_multiplier(long_gap), 1.0)

    def test_thresholds_are_monotonically_ordered(self):
        # Each successive threshold should give a higher or equal multiplier
        thresholds = DEFAULT_XP_SETTINGS.cooldown_thresholds_seconds
        multipliers = [cooldown_multiplier(t - 0.1) for t in thresholds]
        for i in range(len(multipliers) - 1):
            self.assertLessEqual(multipliers[i], multipliers[i + 1])


class PairMultiplierTests(unittest.TestCase):
    def test_below_threshold_returns_one(self):
        below = DEFAULT_XP_SETTINGS.pair_streak_threshold - 1
        self.assertEqual(pair_multiplier(below), 1.0)

    def test_at_threshold_applies_multiplier(self):
        at = DEFAULT_XP_SETTINGS.pair_streak_threshold
        self.assertEqual(pair_multiplier(at), DEFAULT_XP_SETTINGS.pair_streak_multiplier)

    def test_above_threshold_applies_multiplier(self):
        above = DEFAULT_XP_SETTINGS.pair_streak_threshold + 5
        self.assertEqual(pair_multiplier(above), DEFAULT_XP_SETTINGS.pair_streak_multiplier)

    def test_zero_streak_returns_one(self):
        self.assertEqual(pair_multiplier(0), 1.0)


class RoleGrantDueTests(unittest.TestCase):
    threshold = DEFAULT_XP_SETTINGS.role_grant_level

    def test_crossing_threshold_is_due(self):
        self.assertTrue(role_grant_due(self.threshold - 1, self.threshold))

    def test_already_above_threshold_is_not_due(self):
        self.assertFalse(role_grant_due(self.threshold, self.threshold + 1))

    def test_both_below_threshold_is_not_due(self):
        self.assertFalse(role_grant_due(self.threshold - 2, self.threshold - 1))

    def test_no_level_change_is_not_due(self):
        self.assertFalse(role_grant_due(self.threshold, self.threshold))


class UpdatePairStateTests(unittest.TestCase):
    def test_first_message_returns_zero_streak(self):
        new_state, streak = update_pair_state(None, author_id=1)
        self.assertEqual(streak, 0)
        self.assertEqual(new_state.last_author_id, 1)

    def test_same_author_twice_resets_streak(self):
        state, _ = update_pair_state(None, author_id=1)
        new_state, streak = update_pair_state(state, author_id=1)
        self.assertEqual(streak, 0)

    def test_alternating_authors_builds_streak(self):
        state, _ = update_pair_state(None, author_id=1)
        state, streak1 = update_pair_state(state, author_id=2)
        state, streak2 = update_pair_state(state, author_id=1)
        state, streak3 = update_pair_state(state, author_id=2)
        self.assertEqual(streak1, 1)
        self.assertEqual(streak2, 2)
        self.assertEqual(streak3, 3)

    def test_new_third_party_resets_streak(self):
        state, _ = update_pair_state(None, author_id=1)
        state, _ = update_pair_state(state, author_id=2)
        state, _ = update_pair_state(state, author_id=1)  # streak=2
        new_state, streak = update_pair_state(state, author_id=3)  # different pair
        self.assertEqual(streak, 1)  # new pair, starts fresh


class CompletedVoiceIntervalsTests(unittest.TestCase):
    interval = DEFAULT_XP_SETTINGS.voice_interval_seconds

    def _session(self, qualified_since, awarded_intervals=0):
        return VoiceSession(
            guild_id=1,
            user_id=1,
            channel_id=10,
            session_started_at=qualified_since or 0.0,
            qualified_since=qualified_since,
            awarded_intervals=awarded_intervals,
        )

    def test_not_qualified_returns_zero(self):
        session = self._session(qualified_since=None)
        self.assertEqual(completed_voice_intervals(session, now_ts=9999.0), 0)

    def test_not_enough_time_returns_zero(self):
        session = self._session(qualified_since=1000.0)
        self.assertEqual(completed_voice_intervals(session, now_ts=1000.0 + self.interval - 1), 0)

    def test_exactly_one_interval_returns_one(self):
        session = self._session(qualified_since=1000.0)
        self.assertEqual(completed_voice_intervals(session, now_ts=1000.0 + self.interval), 1)

    def test_already_awarded_intervals_are_subtracted(self):
        session = self._session(qualified_since=1000.0, awarded_intervals=2)
        now = 1000.0 + self.interval * 5
        self.assertEqual(completed_voice_intervals(session, now_ts=now), 3)

    def test_all_intervals_already_awarded_returns_zero(self):
        session = self._session(qualified_since=1000.0, awarded_intervals=5)
        now = 1000.0 + self.interval * 5
        self.assertEqual(completed_voice_intervals(session, now_ts=now), 0)


class VoiceSessionDbTests(unittest.TestCase):
    def test_get_missing_session_returns_none(self):
        conn = make_conn()
        self.assertIsNone(get_voice_session(conn, guild_id=1, user_id=99))

    def test_set_and_get_session(self):
        conn = make_conn()
        set_voice_session(conn, 1, 10, 100, session_started_at=500.0, qualified_since=600.0, awarded_intervals=3)
        session = get_voice_session(conn, guild_id=1, user_id=10)
        self.assertIsNotNone(session)
        self.assertEqual(session.channel_id, 100)
        self.assertEqual(session.session_started_at, 500.0)
        self.assertEqual(session.qualified_since, 600.0)
        self.assertEqual(session.awarded_intervals, 3)

    def test_set_session_upserts(self):
        conn = make_conn()
        set_voice_session(conn, 1, 10, 100, session_started_at=500.0)
        set_voice_session(conn, 1, 10, 200, session_started_at=999.0, awarded_intervals=7)
        session = get_voice_session(conn, guild_id=1, user_id=10)
        self.assertEqual(session.channel_id, 200)
        self.assertEqual(session.awarded_intervals, 7)

    def test_qualified_since_can_be_none(self):
        conn = make_conn()
        set_voice_session(conn, 1, 10, 100, session_started_at=500.0, qualified_since=None)
        session = get_voice_session(conn, guild_id=1, user_id=10)
        self.assertIsNone(session.qualified_since)

    def test_delete_session(self):
        conn = make_conn()
        set_voice_session(conn, 1, 10, 100, session_started_at=500.0)
        delete_voice_session(conn, guild_id=1, user_id=10)
        self.assertIsNone(get_voice_session(conn, guild_id=1, user_id=10))

    def test_list_voice_sessions(self):
        conn = make_conn()
        set_voice_session(conn, 1, 10, 100, session_started_at=500.0)
        set_voice_session(conn, 1, 20, 100, session_started_at=600.0)
        set_voice_session(conn, 2, 10, 200, session_started_at=700.0)
        sessions = list_voice_sessions(conn)
        self.assertEqual(len(sessions), 3)
        user_ids = {s.user_id for s in sessions}
        self.assertEqual(user_ids, {10, 20})

    def test_list_voice_sessions_empty(self):
        conn = make_conn()
        self.assertEqual(list_voice_sessions(conn), [])


class XpAggregateQueryTests(unittest.TestCase):
    def test_has_any_xp_events_false_when_empty(self):
        conn = make_conn()
        self.assertFalse(has_any_xp_events(conn, guild_id=1))

    def test_has_any_xp_events_true_after_event(self):
        conn = make_conn()
        record_xp_event(conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=100.0)
        self.assertTrue(has_any_xp_events(conn, guild_id=1))

    def test_has_any_xp_events_scoped_to_guild(self):
        conn = make_conn()
        record_xp_event(conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=100.0)
        self.assertFalse(has_any_xp_events(conn, guild_id=2))

    def test_has_any_member_xp_false_when_empty(self):
        conn = make_conn()
        self.assertFalse(has_any_member_xp(conn, guild_id=1))

    def test_has_any_member_xp_true_after_award(self):
        conn = make_conn()
        apply_xp_award(conn, guild_id=1, user_id=1, xp_delta=10.0, settings=DEFAULT_XP_SETTINGS)
        self.assertTrue(has_any_member_xp(conn, guild_id=1))

    def test_has_any_member_xp_scoped_to_guild(self):
        conn = make_conn()
        apply_xp_award(conn, guild_id=1, user_id=1, xp_delta=10.0, settings=DEFAULT_XP_SETTINGS)
        self.assertFalse(has_any_member_xp(conn, guild_id=2))

    def test_count_xp_events_empty(self):
        conn = make_conn()
        self.assertEqual(count_xp_events(conn, guild_id=1), 0)

    def test_count_xp_events_counts_all_sources(self):
        conn = make_conn()
        record_xp_event(conn, guild_id=1, user_id=1, source=XP_SOURCE_TEXT, amount=5.0, created_at=100.0)
        record_xp_event(conn, guild_id=1, user_id=1, source=XP_SOURCE_VOICE, amount=3.0, created_at=200.0)
        record_xp_event(conn, guild_id=2, user_id=1, source=XP_SOURCE_TEXT, amount=1.0, created_at=300.0)
        self.assertEqual(count_xp_events(conn, guild_id=1), 2)
        self.assertEqual(count_xp_events(conn, guild_id=2), 1)


if __name__ == "__main__":
    unittest.main()
