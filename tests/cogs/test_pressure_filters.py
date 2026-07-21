"""Unit tests for pressure_cooker/filters.py — pure logic, no Discord."""
from __future__ import annotations

from bot_modules.duels.filters import (
    WAGER_STAKES_TEXT,
    contains_disallowed_content,
    resolve_stakes_text,
    validate_nickname,
    validate_stakes,
)


# ── contains_disallowed_content (free-text guard: RR question/reply, confess) ──


def test_contains_disallowed_content_passes_clean_text():
    assert contains_disallowed_content("what's your favorite dessert?") is False


def test_contains_disallowed_content_flags_caller_denylist_case_insensitively():
    # Uses a caller-supplied pattern so no real slur is needed in the test.
    assert contains_disallowed_content("please frobnicate", denylist=["frobnicate"]) is True
    assert contains_disallowed_content("FROBNICATE now", denylist=["frobnicate"]) is True


# ── validate_nickname ────────────────────────────────────────────────────────

def test_nickname_ok():
    r = validate_nickname("CoolDude", max_length=32)
    assert r.ok
    assert r.value == "CoolDude"
    assert r.reason is None


def test_nickname_strips_whitespace():
    r = validate_nickname("  hello  ", max_length=32)
    assert r.ok
    assert r.value == "hello"


def test_nickname_too_long():
    r = validate_nickname("x" * 33, max_length=32)
    assert not r.ok
    assert "32" in r.reason


def test_nickname_blank_after_clean():
    r = validate_nickname("   ", max_length=32)
    assert not r.ok
    assert "blank" in r.reason.lower()


def test_nickname_zero_width_chars_stripped():
    # zero-width space embedded — should be stripped before length/denylist checks
    raw = "Cool​Dude"
    r = validate_nickname(raw, max_length=32)
    assert r.ok
    assert "​" not in r.value


def test_nickname_denylist_default_hit():
    r = validate_nickname("nigger", max_length=32)
    assert not r.ok
    assert "disallowed" in r.reason.lower()


def test_nickname_denylist_custom_hit():
    r = validate_nickname("badword", max_length=32, denylist=[r"\bbadword\b"])
    assert not r.ok
    assert "disallowed" in r.reason.lower()


def test_nickname_at_prefix_rejected():
    r = validate_nickname("@everyone", max_length=32)
    assert not r.ok
    assert "@" in r.reason


def test_nickname_hash_prefix_rejected():
    r = validate_nickname("#channel", max_length=32)
    assert not r.ok


def test_nickname_slash_prefix_rejected():
    r = validate_nickname("/admin", max_length=32)
    assert not r.ok


def test_nickname_everyone_token_rejected():
    r = validate_nickname("hello everyone", max_length=32)
    assert not r.ok
    assert "everyone" in r.reason.lower()


def test_nickname_here_token_rejected():
    r = validate_nickname("ping here please", max_length=32)
    assert not r.ok
    assert "here" in r.reason.lower()


def test_nickname_everyone_in_word_allowed():
    # "everyone" only rejected as a whole word
    r = validate_nickname("noteveryone123", max_length=32)
    assert r.ok


def test_nickname_admin_impersonation_rejected():
    r = validate_nickname("AdminBob", max_length=32, admin_display_names=["AdminBob"])
    assert not r.ok
    assert "impersonat" in r.reason.lower()


def test_nickname_admin_impersonation_case_insensitive():
    r = validate_nickname("adminbob", max_length=32, admin_display_names=["AdminBob"])
    assert not r.ok


def test_nickname_member_exact_match_rejected():
    r = validate_nickname("Alice", max_length=32, all_member_display_names=["Alice"])
    assert not r.ok
    assert "taken" in r.reason.lower()


def test_nickname_member_partial_match_allowed():
    r = validate_nickname("Alic", max_length=32, all_member_display_names=["Alice"])
    assert r.ok


def test_nickname_admin_name_not_in_member_list_allowed():
    # admin list is separate from member list; admin check runs first
    r = validate_nickname(
        "SafeName",
        max_length=32,
        admin_display_names=["AdminOnly"],
        all_member_display_names=["SafeName"],
    )
    assert not r.ok  # member list catches it


def test_nickname_max_length_exact_boundary():
    r = validate_nickname("x" * 32, max_length=32)
    assert r.ok


# ── validate_stakes ───────────────────────────────────────────────────────────

def test_stakes_ok():
    r = validate_stakes("Loser buys pizza", max_length=200)
    assert r.ok
    assert r.value == "Loser buys pizza"


def test_stakes_too_long():
    r = validate_stakes("x" * 201, max_length=200)
    assert not r.ok
    assert "200" in r.reason


def test_stakes_denylist_hit():
    r = validate_stakes("nigger", max_length=200)
    assert not r.ok


def test_stakes_admin_name_allowed():
    # stakes has no impersonation check
    r = validate_stakes(
        "AdminBob pays for drinks",
        max_length=200,
    )
    assert r.ok


def test_stakes_strips_whitespace():
    r = validate_stakes("  pizza  ", max_length=200)
    assert r.ok
    assert r.value == "pizza"


def test_stakes_empty_string_ok():
    # empty stakes is valid (means no custom stakes)
    r = validate_stakes("", max_length=200)
    assert r.ok


# ── resolve_stakes_text ────────────────────────────────────────────────────────
# A game is in nickname mode iff its persisted stakes_text is None. This helper
# is what keeps a wagered game *out* of that mode: the loser forfeits coins, not
# their nickname.

def test_resolve_plain_game_stays_nick_mode():
    # No custom stakes, no wager → None persists → nickname mode (the default).
    assert resolve_stakes_text(None, None) is None


def test_resolve_wager_becomes_a_stakes_label():
    # A wager with no custom stakes gets the label, flipping it out of nick mode.
    assert resolve_stakes_text(None, 100) == WAGER_STAKES_TEXT


def test_resolve_custom_stakes_pass_through_without_wager():
    assert resolve_stakes_text("Loser buys pizza", None) == "Loser buys pizza"


def test_resolve_custom_stakes_win_over_wager():
    # If the host typed real stakes, keep them even alongside a wager.
    assert resolve_stakes_text("Loser buys pizza", 100) == "Loser buys pizza"
