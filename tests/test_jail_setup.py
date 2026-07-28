"""Tests for the pure helpers in ``bot_modules.jail.logic``: channel-name
sanitization and mention-list capping.

The ``/setup`` wizard's step metadata was covered here too until 2026-07-28,
when the command was removed — all six settings it walked through already had
a home on Config → Moderation.

The original ``logic.py`` (snapshot/restore, eligible_voters, vote_outcome)
is covered by ``test_jail_role_logic.py`` and ``test_jail_apply.py``. This
file is scoped to the new entry points.
"""

from __future__ import annotations


from bot_modules.jail.logic import (
    cap_mentions,
    sanitize_channel_name,
)


# ── sanitize_channel_name ──────────────────────────────────────────────


def test_sanitize_lowercases_input():
    assert sanitize_channel_name("Hello") == "hello"


def test_sanitize_replaces_runs_of_invalid_chars_with_single_hyphen():
    """Multiple consecutive invalid chars become a *single* hyphen — Discord
    accepts ``foo-bar`` but not ``foo  bar``."""
    assert sanitize_channel_name("hello world") == "hello-world"
    assert sanitize_channel_name("foo!!!bar") == "foo-bar"


def test_sanitize_preserves_allowed_chars():
    """Lowercase letters, digits, underscore, hyphen all pass through."""
    assert sanitize_channel_name("user_42-test") == "user_42-test"


def test_sanitize_strips_edge_hyphens():
    """Discord rejects channel names that start or end with a hyphen."""
    assert sanitize_channel_name("!!!hello!!!") == "hello"
    assert sanitize_channel_name("- -hello- -") == "hello"


def test_sanitize_empty_input_returns_fallback():
    assert sanitize_channel_name("") == "user"


def test_sanitize_only_invalid_chars_returns_fallback():
    """A name made entirely of invalid chars degenerates to empty after the
    sub + strip — the fallback prevents an empty interpolation in the cog."""
    assert sanitize_channel_name("!!!@@@") == "user"


def test_sanitize_custom_fallback():
    assert sanitize_channel_name("", fallback="anon") == "anon"


def test_sanitize_unicode_falls_back_or_strips():
    """Unicode letters (é, ñ, 中) are not in the allowed ASCII set and get
    replaced. The cog uses this on Discord usernames which may contain them."""
    assert sanitize_channel_name("café") == "caf"
    assert sanitize_channel_name("中文") == "user"


# ── cap_mentions ───────────────────────────────────────────────────────


def test_cap_under_limit_returns_all_sorted():
    shown, overflow = cap_mentions([3, 1, 2], max_count=10)
    assert shown == [1, 2, 3]
    assert overflow == 0


def test_cap_at_limit_returns_all():
    shown, overflow = cap_mentions([1, 2, 3], max_count=3)
    assert shown == [1, 2, 3]
    assert overflow == 0


def test_cap_over_limit_truncates_and_reports_overflow():
    shown, overflow = cap_mentions([5, 4, 3, 2, 1], max_count=2)
    assert shown == [1, 2]
    assert overflow == 3


def test_cap_accepts_a_set():
    """The cog's ``eligible`` is a set; the helper has to handle that."""
    shown, overflow = cap_mentions({30, 10, 20}, max_count=10)
    assert shown == [10, 20, 30]
    assert overflow == 0


def test_cap_default_is_25():
    big = list(range(40))
    shown, overflow = cap_mentions(big)
    assert len(shown) == 25
    assert overflow == 15


def test_cap_empty_input():
    shown, overflow = cap_mentions([])
    assert shown == []
    assert overflow == 0
