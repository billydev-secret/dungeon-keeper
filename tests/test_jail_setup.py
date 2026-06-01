"""Tests for the pure helpers added to ``bot_modules.jail.logic`` to support
the cog refactor: channel-name sanitization, mention-list capping, and the
``/setup`` wizard step metadata.

The original ``logic.py`` (snapshot/restore, eligible_voters, vote_outcome)
is covered by ``test_jail_role_logic.py`` and ``test_jail_apply.py``. This
file is scoped to the new entry points.
"""

from __future__ import annotations

import pytest

from bot_modules.jail.logic import (
    SETUP_FINAL_STEP,
    cap_mentions,
    sanitize_channel_name,
    setup_button_label,
    setup_step_meta,
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


# ── setup_step_meta ────────────────────────────────────────────────────


def test_setup_step_meta_step_1_mod_roles():
    meta = setup_step_meta(1)
    assert meta is not None
    assert meta["title"] == "Setup — Step 1/6"
    assert "moderator" in meta["description"]
    assert meta["config_key"] == "mod_role_ids"
    assert meta["select_kind"] == "role"


def test_setup_step_meta_step_2_admin_roles():
    meta = setup_step_meta(2)
    assert meta is not None
    assert meta["config_key"] == "admin_role_ids"
    assert "admin" in meta["description"]
    assert meta["select_kind"] == "role"


def test_setup_step_meta_step_3_jail_category():
    meta = setup_step_meta(3)
    assert meta is not None
    assert meta["config_key"] == "jail_category_id"
    assert meta["select_kind"] == "category"
    assert "jail channels" in meta["description"]


def test_setup_step_meta_step_4_ticket_category():
    meta = setup_step_meta(4)
    assert meta is not None
    assert meta["config_key"] == "ticket_category_id"
    assert meta["select_kind"] == "category"


def test_setup_step_meta_step_5_log_channel():
    meta = setup_step_meta(5)
    assert meta is not None
    assert meta["config_key"] == "log_channel_id"
    assert meta["select_kind"] == "channel"


def test_setup_step_meta_step_6_transcript_channel():
    meta = setup_step_meta(6)
    assert meta is not None
    assert meta["config_key"] == "transcript_channel_id"
    assert meta["select_kind"] == "channel"
    assert "transcripts" in meta["description"]


@pytest.mark.parametrize("step", [0, 7, 8, -1, 99])
def test_setup_step_meta_out_of_range_returns_none(step):
    """Steps outside the 1..6 range signal "we're done" — the cog renders
    the completion embed when this returns None."""
    assert setup_step_meta(step) is None


def test_setup_step_meta_returns_copy_so_caller_cannot_mutate():
    """Mutating the returned dict must not affect later lookups."""
    meta = setup_step_meta(1)
    assert meta is not None
    meta["title"] = "HAXX"
    meta2 = setup_step_meta(1)
    assert meta2 is not None
    assert meta2["title"] == "Setup — Step 1/6"


def test_setup_final_step_constant():
    assert SETUP_FINAL_STEP == 6


def test_setup_button_label_intermediate_step():
    assert setup_button_label(1) == "Next →"
    assert setup_button_label(5) == "Next →"


def test_setup_button_label_final_step():
    assert setup_button_label(6) == "Finish"


def test_setup_button_label_post_final():
    """Past the final step we'd say "Finish" too, though the caller is
    expected to stop calling once meta returns None."""
    assert setup_button_label(7) == "Finish"
