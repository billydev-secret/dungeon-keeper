import unittest
from types import SimpleNamespace

from utils import format_user_for_log, resolve_user_for_log


class FormatUserForLogTests(unittest.TestCase):
    def test_no_args_returns_unknown(self):
        self.assertEqual(format_user_for_log(), "unknown user")

    def test_user_id_only(self):
        self.assertEqual(format_user_for_log(user_id=42), "user 42")

    def test_display_name_matches_username(self):
        user = SimpleNamespace(id=1, display_name="Alice", name="Alice")
        self.assertEqual(format_user_for_log(user), "Alice (1)")

    def test_display_name_differs_from_username(self):
        user = SimpleNamespace(id=1, display_name="Wonderland Alice", name="alice99")
        self.assertEqual(format_user_for_log(user), "Wonderland Alice [alice99] (1)")

    def test_display_name_none_falls_back_to_username(self):
        user = SimpleNamespace(id=5, display_name=None, name="bob")
        self.assertEqual(format_user_for_log(user), "bob (5)")

    def test_user_overrides_user_id(self):
        user = SimpleNamespace(id=10, display_name="Carol", name="Carol")
        self.assertEqual(format_user_for_log(user, user_id=99), "Carol (10)")

    def test_user_with_no_id_uses_fallback_id(self):
        # display_name != name triggers bracket format; id falls back to user_id kwarg
        user = SimpleNamespace(display_name="Dave", name="dave")
        self.assertEqual(format_user_for_log(user, user_id=7), "Dave [dave] (7)")


class ResolveUserForLogTests(unittest.TestCase):
    def test_known_member_uses_format(self):
        member = SimpleNamespace(id=10, display_name="Eve", name="Eve")
        guild = SimpleNamespace(get_member=lambda uid: member if uid == 10 else None)
        self.assertEqual(resolve_user_for_log(guild, 10), "Eve (10)")

    def test_unknown_member_falls_back_to_id(self):
        guild = SimpleNamespace(get_member=lambda uid: None)
        self.assertEqual(resolve_user_for_log(guild, 99), "user 99")

    def test_none_guild_falls_back_to_id(self):
        self.assertEqual(resolve_user_for_log(None, 42), "user 42")


if __name__ == "__main__":
    unittest.main()
