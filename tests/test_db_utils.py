import shutil
import tempfile
import unittest
from pathlib import Path

from db_utils import get_config_id_set, get_config_value, init_config_db, open_db, parse_bool


class ParseBoolTests(unittest.TestCase):
    def test_truthy_strings(self):
        for value in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
            with self.subTest(value=value):
                self.assertTrue(parse_bool(value))

    def test_falsy_strings(self):
        for value in ("0", "false", "False", "no", "off", "random", ""):
            with self.subTest(value=value):
                self.assertFalse(parse_bool(value))

    def test_none_returns_default_false(self):
        self.assertFalse(parse_bool(None))

    def test_none_returns_explicit_default(self):
        self.assertTrue(parse_bool(None, default=True))
        self.assertFalse(parse_bool(None, default=False))

    def test_strips_whitespace(self):
        self.assertTrue(parse_bool("  true  "))
        self.assertFalse(parse_bool("  false  "))


class ConfigDbTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self._tmpdir) / "test.db"
        init_config_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_get_config_value_missing_key_returns_default(self):
        with open_db(self.db_path) as conn:
            self.assertEqual(get_config_value(conn, "missing", "fallback"), "fallback")

    def test_get_config_value_stored_key(self):
        with open_db(self.db_path) as conn:
            conn.execute("INSERT INTO config (key, value) VALUES ('mykey', 'myval')")
            self.assertEqual(get_config_value(conn, "mykey", "fallback"), "myval")

    def test_get_config_value_overrides_default(self):
        with open_db(self.db_path) as conn:
            conn.execute("INSERT INTO config (key, value) VALUES ('guild_id', '12345')")
            self.assertEqual(get_config_value(conn, "guild_id", "0"), "12345")

    def test_get_config_id_set_empty_bucket(self):
        with open_db(self.db_path) as conn:
            self.assertEqual(get_config_id_set(conn, "no_such_bucket"), set())

    def test_get_config_id_set_returns_correct_ids(self):
        with open_db(self.db_path) as conn:
            conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('roles', 10)")
            conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('roles', 20)")
            conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('other', 99)")
            self.assertEqual(get_config_id_set(conn, "roles"), {10, 20})

    def test_get_config_id_set_scoped_to_bucket(self):
        with open_db(self.db_path) as conn:
            conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('a', 1)")
            conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('b', 2)")
            self.assertEqual(get_config_id_set(conn, "a"), {1})
            self.assertEqual(get_config_id_set(conn, "b"), {2})

    def test_init_config_db_is_idempotent(self):
        # Calling again should not raise or corrupt existing data
        with open_db(self.db_path) as conn:
            conn.execute("INSERT INTO config (key, value) VALUES ('test', 'val')")
        init_config_db(self.db_path)
        with open_db(self.db_path) as conn:
            self.assertEqual(get_config_value(conn, "test", "missing"), "val")


if __name__ == "__main__":
    unittest.main()
