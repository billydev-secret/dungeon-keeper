import sqlite3
import unittest

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


class XpSystemTests(unittest.TestCase):
    def test_qualified_words_filters_urls_and_junk(self):
        words = qualified_words(
            "hi wow!!! https://example.com 😀 <:wave:123> alpha beta2 ..."
        )
        self.assertEqual(words, ["wow", "alpha", "beta2"])

    def test_calculate_message_xp_applies_all_modifiers(self):
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

        self.assertEqual(breakdown.qualified_words, 4)
        self.assertEqual(breakdown.normalized_content, "alpha beta gamma delta")
        self.assertEqual(breakdown.awarded_xp, 0.02)

    def test_apply_xp_award_levels_up_and_marks_role_reward(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        level_5_threshold = xp_required_for_level(
            DEFAULT_XP_SETTINGS.role_grant_level, DEFAULT_XP_SETTINGS
        )
        first = apply_xp_award(
            conn,
            guild_id=1,
            user_id=42,
            xp_delta=level_5_threshold - 0.01,
            settings=DEFAULT_XP_SETTINGS,
        )
        second = apply_xp_award(
            conn,
            guild_id=1,
            user_id=42,
            xp_delta=0.01,
            message_timestamp=123.0,
            message_norm="alpha beta",
            settings=DEFAULT_XP_SETTINGS,
        )
        state = get_member_xp_state(conn, guild_id=1, user_id=42)

        self.assertEqual(first.new_level, 4)
        self.assertFalse(first.role_grant_due)
        self.assertEqual(second.new_level, 5)
        self.assertTrue(second.role_grant_due)
        self.assertEqual(state.total_xp, level_5_threshold)
        self.assertEqual(state.level, 5)
        self.assertEqual(state.last_message_at, 123.0)
        self.assertEqual(state.last_message_norm, "alpha beta")

    def test_level_for_xp_uses_sqrt_thresholds(self):
        level_4_threshold = xp_required_for_level(4, DEFAULT_XP_SETTINGS)
        level_5_threshold = xp_required_for_level(5, DEFAULT_XP_SETTINGS)

        self.assertEqual(level_4_threshold, 140.4)
        self.assertEqual(level_5_threshold, 249.6)
        self.assertEqual(level_for_xp(level_4_threshold - 0.01, DEFAULT_XP_SETTINGS), 3)
        self.assertEqual(level_for_xp(level_4_threshold, DEFAULT_XP_SETTINGS), 4)
        self.assertEqual(level_for_xp(level_5_threshold - 0.01, DEFAULT_XP_SETTINGS), 4)
        self.assertEqual(level_for_xp(level_5_threshold, DEFAULT_XP_SETTINGS), 5)

    def test_get_member_xp_state_recalculates_cached_level_from_total_xp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        conn.execute(
            """
            INSERT INTO member_xp (guild_id, user_id, total_xp, level, last_message_at, last_message_norm)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1, 42, xp_required_for_level(5, DEFAULT_XP_SETTINGS), 2, 123.0, "alpha"),
        )

        state = get_member_xp_state(
            conn, guild_id=1, user_id=42, settings=DEFAULT_XP_SETTINGS
        )

        self.assertEqual(state.level, 5)
        self.assertEqual(state.total_xp, xp_required_for_level(5, DEFAULT_XP_SETTINGS))
        self.assertEqual(state.last_message_at, 123.0)
        self.assertEqual(state.last_message_norm, "alpha")

    def test_channel_xp_is_enabled_by_default_and_blocked_when_excluded(self):
        self.assertTrue(
            is_channel_xp_eligible(
                channel_id=10, parent_id=None, excluded_channel_ids=set()
            )
        )
        self.assertFalse(
            is_channel_xp_eligible(
                channel_id=10, parent_id=None, excluded_channel_ids={10}
            )
        )
        self.assertFalse(
            is_channel_xp_eligible(
                channel_id=11, parent_id=10, excluded_channel_ids={10}
            )
        )

    def test_leaderboard_filters_by_source_and_time_window(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        apply_xp_award(
            conn,
            guild_id=1,
            user_id=10,
            xp_delta=50.0,
            event_source=XP_SOURCE_TEXT,
            event_timestamp=1_000.0,
            settings=DEFAULT_XP_SETTINGS,
        )
        apply_xp_award(
            conn,
            guild_id=1,
            user_id=11,
            xp_delta=20.0,
            event_source=XP_SOURCE_TEXT,
            event_timestamp=2_000.0,
            settings=DEFAULT_XP_SETTINGS,
        )
        apply_xp_award(
            conn,
            guild_id=1,
            user_id=11,
            xp_delta=80.0,
            event_source=XP_SOURCE_VOICE,
            event_timestamp=2_000.0,
            settings=DEFAULT_XP_SETTINGS,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=12,
            source=XP_SOURCE_REPLY,
            amount=7.5,
            created_at=2_500.0,
        )

        all_time_text = get_xp_leaderboard(
            conn, guild_id=1, source=XP_SOURCE_TEXT, limit=5
        )
        recent_text = get_xp_leaderboard(
            conn, guild_id=1, source=XP_SOURCE_TEXT, since_ts=1_500.0, limit=5
        )
        reply_board = get_xp_leaderboard(
            conn, guild_id=1, source=XP_SOURCE_REPLY, limit=5
        )

        self.assertEqual(
            [(entry.user_id, entry.xp) for entry in all_time_text],
            [(10, 50.0), (11, 20.0)],
        )
        self.assertEqual(
            [(entry.user_id, entry.xp) for entry in recent_text], [(11, 20.0)]
        )
        self.assertEqual(
            [(entry.user_id, entry.xp) for entry in reply_board], [(12, 7.5)]
        )

    def test_xp_distribution_stats_report_member_count_median_and_stddev(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        record_xp_event(
            conn,
            guild_id=1,
            user_id=10,
            source=XP_SOURCE_TEXT,
            amount=10.0,
            created_at=100.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=11,
            source=XP_SOURCE_TEXT,
            amount=20.0,
            created_at=100.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=12,
            source=XP_SOURCE_TEXT,
            amount=30.0,
            created_at=100.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=12,
            source=XP_SOURCE_TEXT,
            amount=10.0,
            created_at=150.0,
        )

        stats = get_xp_distribution_stats(conn, guild_id=1, source=XP_SOURCE_TEXT)
        recent_stats = get_xp_distribution_stats(
            conn, guild_id=1, source=XP_SOURCE_TEXT, since_ts=125.0
        )

        self.assertEqual(
            (stats.member_count, stats.median_xp, stats.stddev_xp), (3, 20.0, 12.47)
        )
        self.assertEqual(
            (recent_stats.member_count, recent_stats.median_xp, recent_stats.stddev_xp),
            (1, 10.0, 0.0),
        )

    def test_processed_message_tracking_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        self.assertFalse(is_message_processed(conn, guild_id=1, message_id=123))
        mark_message_processed(
            conn,
            guild_id=1,
            message_id=123,
            channel_id=10,
            user_id=20,
            created_at=1000.0,
        )
        mark_message_processed(
            conn,
            guild_id=1,
            message_id=123,
            channel_id=10,
            user_id=20,
            created_at=1000.0,
        )

        self.assertTrue(is_message_processed(conn, guild_id=1, message_id=123))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0], 1
        )

    def test_member_activity_keeps_latest_message_per_member(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        record_member_activity(
            conn,
            guild_id=1,
            user_id=10,
            channel_id=100,
            message_id=1000,
            created_at=200.0,
        )
        record_member_activity(
            conn,
            guild_id=1,
            user_id=10,
            channel_id=101,
            message_id=1001,
            created_at=150.0,
        )
        record_member_activity(
            conn,
            guild_id=1,
            user_id=10,
            channel_id=102,
            message_id=1002,
            created_at=300.0,
        )
        record_member_activity(
            conn,
            guild_id=1,
            user_id=11,
            channel_id=103,
            message_id=1003,
            created_at=250.0,
        )

        activities = get_member_last_activity_map(
            conn, guild_id=1, user_ids=[10, 11, 12]
        )

        self.assertEqual(sorted(activities.keys()), [10, 11])
        self.assertEqual(
            (
                activities[10].channel_id,
                activities[10].message_id,
                activities[10].created_at,
            ),
            (102, 1002, 300.0),
        )
        self.assertEqual(
            (
                activities[11].channel_id,
                activities[11].message_id,
                activities[11].created_at,
            ),
            (103, 1003, 250.0),
        )

    def test_oldest_xp_event_timestamp_filters_by_source(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        record_xp_event(
            conn,
            guild_id=1,
            user_id=1,
            source=XP_SOURCE_VOICE,
            amount=5.0,
            created_at=300.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=1,
            source=XP_SOURCE_TEXT,
            amount=5.0,
            created_at=200.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=1,
            source=XP_SOURCE_REPLY,
            amount=5.0,
            created_at=100.0,
        )

        self.assertEqual(get_oldest_xp_event_timestamp(conn, guild_id=1), 100.0)
        self.assertEqual(
            get_oldest_xp_event_timestamp(
                conn, guild_id=1, sources=(XP_SOURCE_TEXT, XP_SOURCE_REPLY)
            ),
            100.0,
        )
        self.assertEqual(
            get_oldest_xp_event_timestamp(conn, guild_id=1, sources=(XP_SOURCE_VOICE,)),
            300.0,
        )

    def test_user_xp_standing_reports_rank_and_missing_user(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        record_xp_event(
            conn,
            guild_id=1,
            user_id=10,
            source=XP_SOURCE_TEXT,
            amount=12.0,
            created_at=100.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=11,
            source=XP_SOURCE_TEXT,
            amount=20.0,
            created_at=100.0,
        )
        record_xp_event(
            conn,
            guild_id=1,
            user_id=12,
            source=XP_SOURCE_TEXT,
            amount=12.0,
            created_at=100.0,
        )

        standing = get_user_xp_standing(
            conn, guild_id=1, source=XP_SOURCE_TEXT, user_id=12
        )
        missing = get_user_xp_standing(
            conn, guild_id=1, source=XP_SOURCE_TEXT, user_id=99
        )

        self.assertEqual((standing.rank, standing.xp), (3, 12.0))
        self.assertEqual((missing.rank, missing.xp), (None, 0.0))

    def test_manual_grant_source_records_event(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_xp_tables(conn)

        award = apply_xp_award(
            conn,
            guild_id=1,
            user_id=50,
            xp_delta=DEFAULT_XP_SETTINGS.manual_grant_xp,
            event_source=XP_SOURCE_GRANT,
            event_timestamp=500.0,
            settings=DEFAULT_XP_SETTINGS,
        )
        board = get_xp_leaderboard(conn, guild_id=1, source=XP_SOURCE_GRANT, limit=5)

        self.assertEqual(award.awarded_xp, DEFAULT_XP_SETTINGS.manual_grant_xp)
        self.assertEqual(
            [(entry.user_id, entry.xp) for entry in board],
            [(50, DEFAULT_XP_SETTINGS.manual_grant_xp)],
        )


if __name__ == "__main__":
    unittest.main()
