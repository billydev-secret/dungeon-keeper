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
    r = validate_nickname("badword", max_length=32, denylist=["badword"])
    assert not r.ok
    assert "disallowed" in r.reason.lower()


def test_nickname_denylist_custom_words_are_literal_not_regex():
    """The extra words come from an admin typing in a dashboard box.

    They are matched as case-insensitive substrings: a word carrying regex
    punctuation ("c++") has to ban that literal text, not blow up or quietly
    never match.
    """
    assert not validate_nickname("totally c++ fine", max_length=32, denylist=["c++"]).ok
    assert validate_nickname("plain", max_length=32, denylist=["c++"]).ok
    # ...and a regex a previous version would have honoured is now just text.
    assert validate_nickname("badword", max_length=32, denylist=[r"\bnope\b"]).ok


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
# The persisted text lists every live stake, one line each, so every embed that
# renders it shows all of them. Nickname mode itself is carried by the separate
# nick_stake flag (migration 177), not inferred from this string.

def test_resolve_plain_game_stays_nick_mode():
    # No custom stakes, no wager → None persists → nickname mode (the default),
    # and every cog's own fallback wording still renders.
    assert resolve_stakes_text(None, None) is None


def test_resolve_wager_becomes_a_stakes_label():
    # A wager with no custom stakes and no rename gets its own line, with the
    # amount on it — the coins used to be invisible until settlement.
    text = resolve_stakes_text(None, 100, nick_stake=False)
    assert text is not None and "100" in text and "winner takes the pot" in text


def test_resolve_lists_every_live_stake():
    """Coins, custom text and the rename are independent since migration 177,
    so a game can carry all three and the field has to show all three."""
    text = resolve_stakes_text("loser sings", 100, nick_stake=True)
    assert text is not None
    assert "loser sings" in text
    assert "100" in text
    assert "nickname" in text.lower()
    assert text.count("\n") == 2  # one line per stake


def test_resolve_uses_a_caller_supplied_wager_line():
    text = resolve_stakes_text(None, 100, nick_stake=False, wager_line="💰 🪙 100 coins each")
    assert text == "💰 🪙 100 coins each"


def test_resolve_custom_stakes_pass_through_without_wager():
    assert resolve_stakes_text("Loser buys pizza", None) == "Loser buys pizza"


def test_resolve_custom_stakes_are_kept_alongside_a_wager():
    # If the host typed real stakes, keep them — and show the wager too.
    text = resolve_stakes_text("Loser buys pizza", 100, nick_stake=False)
    assert text is not None
    assert text.startswith("Loser buys pizza")
    assert "100" in text


def test_resolve_legacy_wager_label_is_still_recognised_as_non_nick():
    """Rows written before migration 177 carry this exact phrase and no flag;
    game_is_nick_stake has to keep reading them as announce-only."""
    from types import SimpleNamespace

    from bot_modules.duels.filters import game_is_nick_stake

    assert not game_is_nick_stake(SimpleNamespace(stakes_text=WAGER_STAKES_TEXT))
    assert game_is_nick_stake(SimpleNamespace(stakes_text=None))
    assert game_is_nick_stake(SimpleNamespace(stakes_text="loser sings", nick_stake=True))
    assert not game_is_nick_stake(SimpleNamespace(stakes_text="loser sings", nick_stake=False))
