import shutil
import tempfile
import unittest
from pathlib import Path

from db_utils import open_db
from services.auto_delete_service import (
    auto_delete_rule_exists,
    format_duration_seconds,
    init_auto_delete_tables,
    list_auto_delete_rules_for_guild,
    parse_duration_seconds,
    pop_due_auto_delete_message_ids,
    remove_auto_delete_rule,
    remove_tracked_auto_delete_message,
    remove_tracked_auto_delete_messages,
    touch_auto_delete_rule_run,
    track_auto_delete_message,
    upsert_auto_delete_rule,
)


class ParseDurationSecondsTests(unittest.TestCase):
    def test_named_intervals(self):
        self.assertEqual(parse_duration_seconds("hourly"), 3600)
        self.assertEqual(parse_duration_seconds("daily"), 86400)
        self.assertEqual(parse_duration_seconds("weekly"), 7 * 86400)

    def test_unit_variants(self):
        self.assertEqual(parse_duration_seconds("30s"), 30)
        self.assertEqual(parse_duration_seconds("30 seconds"), 30)
        self.assertEqual(parse_duration_seconds("5m"), 300)
        self.assertEqual(parse_duration_seconds("5 minutes"), 300)
        self.assertEqual(parse_duration_seconds("2h"), 7200)
        self.assertEqual(parse_duration_seconds("2 hours"), 7200)
        self.assertEqual(parse_duration_seconds("1d"), 86400)
        self.assertEqual(parse_duration_seconds("1w"), 7 * 86400)

    def test_compound_durations(self):
        self.assertEqual(parse_duration_seconds("1h30m"), 5400)
        self.assertEqual(parse_duration_seconds("2d12h"), 2 * 86400 + 12 * 3600)
        self.assertEqual(parse_duration_seconds("1h30m15s"), 5415)

    def test_case_insensitive(self):
        self.assertEqual(parse_duration_seconds("1H"), 3600)
        self.assertEqual(parse_duration_seconds("1D"), 86400)

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_duration_seconds(""))
        self.assertIsNone(parse_duration_seconds("abc"))
        self.assertIsNone(parse_duration_seconds("0h"))
        self.assertIsNone(parse_duration_seconds("1h abc"))
        self.assertIsNone(parse_duration_seconds("abc 1h"))


class FormatDurationSecondsTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_duration_seconds(0), "0s")

    def test_singular_and_plural(self):
        self.assertEqual(format_duration_seconds(60), "1 minute")
        self.assertEqual(format_duration_seconds(120), "2 minutes")
        self.assertEqual(format_duration_seconds(3600), "1 hour")
        self.assertEqual(format_duration_seconds(7200), "2 hours")
        self.assertEqual(format_duration_seconds(86400), "1 day")
        self.assertEqual(format_duration_seconds(172800), "2 days")

    def test_non_round_falls_back_to_seconds(self):
        self.assertEqual(format_duration_seconds(90), "90 seconds")
        self.assertEqual(format_duration_seconds(3661), "3661 seconds")


class AutoDeleteDbTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self._tmpdir) / "test.db"
        with open_db(self.db_path) as conn:
            init_auto_delete_tables(conn)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_upsert_creates_and_lists_rules(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600)
        upsert_auto_delete_rule(self.db_path, 1, 200, 43200, 7200)
        rules = list_auto_delete_rules_for_guild(self.db_path, 1)
        self.assertEqual(len(rules), 2)
        self.assertEqual(int(rules[0]["channel_id"]), 100)
        self.assertEqual(int(rules[1]["channel_id"]), 200)

    def test_upsert_updates_existing_rule(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600)
        upsert_auto_delete_rule(self.db_path, 1, 100, 43200, 7200)
        rules = list_auto_delete_rules_for_guild(self.db_path, 1)
        self.assertEqual(len(rules), 1)
        self.assertEqual(int(rules[0]["max_age_seconds"]), 43200)
        self.assertEqual(int(rules[0]["interval_seconds"]), 7200)

    def test_rules_are_scoped_by_guild(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600)
        upsert_auto_delete_rule(self.db_path, 2, 100, 86400, 3600)
        self.assertEqual(len(list_auto_delete_rules_for_guild(self.db_path, 1)), 1)
        self.assertEqual(len(list_auto_delete_rules_for_guild(self.db_path, 2)), 1)
        self.assertEqual(len(list_auto_delete_rules_for_guild(self.db_path, 3)), 0)

    def test_remove_rule_returns_true_when_found(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600)
        self.assertTrue(remove_auto_delete_rule(self.db_path, 1, 100))
        self.assertEqual(list_auto_delete_rules_for_guild(self.db_path, 1), [])

    def test_remove_rule_returns_false_when_missing(self):
        self.assertFalse(remove_auto_delete_rule(self.db_path, 1, 999))

    def test_auto_delete_rule_exists(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600)
        with open_db(self.db_path) as conn:
            self.assertTrue(auto_delete_rule_exists(conn, 1, 100))
            self.assertFalse(auto_delete_rule_exists(conn, 1, 999))
            self.assertFalse(auto_delete_rule_exists(conn, 2, 100))

    def test_touch_rule_updates_last_run_ts(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600, last_run_ts=0.0)
        touch_auto_delete_rule_run(self.db_path, 1, 100, 9999.0)
        rules = list_auto_delete_rules_for_guild(self.db_path, 1)
        self.assertAlmostEqual(float(rules[0]["last_run_ts"]), 9999.0)

    def test_track_and_pop_due_messages_respects_cutoff(self):
        with open_db(self.db_path) as conn:
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
            track_auto_delete_message(conn, 1, 100, 1002, 200.0)
            track_auto_delete_message(conn, 1, 100, 1003, 300.0)
            due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=250.0)
        self.assertEqual([mid for mid, _ in due], [1001, 1002])

    def test_track_message_is_idempotent(self):
        with open_db(self.db_path) as conn:
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
            due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
        self.assertEqual([mid for mid, _ in due], [1001])

    def test_remove_tracked_message(self):
        with open_db(self.db_path) as conn:
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        remove_tracked_auto_delete_message(self.db_path, 1, 100, 1001)
        with open_db(self.db_path) as conn:
            self.assertEqual(
                pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0), []
            )

    def test_remove_tracked_messages_bulk(self):
        with open_db(self.db_path) as conn:
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
            track_auto_delete_message(conn, 1, 100, 1002, 200.0)
            track_auto_delete_message(conn, 1, 100, 1003, 300.0)
        remove_tracked_auto_delete_messages(self.db_path, 1, 100, {1001, 1002})
        with open_db(self.db_path) as conn:
            self.assertEqual(
                [
                    mid
                    for mid, _ in pop_due_auto_delete_message_ids(
                        conn, 1, 100, cutoff_ts=9999.0
                    )
                ],
                [1003],
            )

    def test_remove_tracked_messages_bulk_empty_set_is_noop(self):
        with open_db(self.db_path) as conn:
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        remove_tracked_auto_delete_messages(self.db_path, 1, 100, set())
        with open_db(self.db_path) as conn:
            self.assertEqual(
                [
                    mid
                    for mid, _ in pop_due_auto_delete_message_ids(
                        conn, 1, 100, cutoff_ts=9999.0
                    )
                ],
                [1001],
            )

    def test_remove_rule_also_clears_tracked_messages(self):
        upsert_auto_delete_rule(self.db_path, 1, 100, 86400, 3600)
        with open_db(self.db_path) as conn:
            track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        remove_auto_delete_rule(self.db_path, 1, 100)
        with open_db(self.db_path) as conn:
            self.assertEqual(
                pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0), []
            )


if __name__ == "__main__":
    unittest.main()
