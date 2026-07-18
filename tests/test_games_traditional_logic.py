"""Tests for the extracted Truth-or-Dare pure-logic modules.

Covers ``bot_modules/games_traditional/logic.py`` (toggle, record,
selection algorithm, recap summarization) and
``bot_modules/games_traditional/embeds.py`` (lobby, recap, question-post
formatter). Mirrors the pressure_cooker pattern: the cog file stays
thin; this module proves the extracted pieces work without spinning
up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games_traditional.embeds import (
    build_lobby_embed,
    build_question_embed,
    build_recap_embed,
    build_tod_embed,
)
from bot_modules.games.utils.question_source import get_traditional_question
from bot_modules.games_traditional.logic import (
    CAT_LABELS,
    CATEGORIES,
    asked_counts_by_user,
    available_targets,
    question_pool_size,
    record_asked,
    select_bank_categories_for_all,
    select_next_question_target,
    summarize_asked_by_category,
    toggle_pref,
)


# ── toggle_pref ──────────────────────────────────────────────────────


def test_toggle_pref_adds_user_and_category_on_first_press():
    payload: dict = {}
    action = toggle_pref(payload, user_id=42, category="sfw_truth")
    assert action == "added"
    assert payload["participants"] == [42]
    assert payload["prefs"] == {"42": ["sfw_truth"]}


def test_toggle_pref_removes_existing_category():
    payload = {"participants": [42], "prefs": {"42": ["sfw_truth", "sfw_dare"]}}
    action = toggle_pref(payload, 42, "sfw_truth")
    assert action == "removed"
    assert payload["prefs"]["42"] == ["sfw_dare"]
    # User still has another preference, so still in participants
    assert payload["participants"] == [42]


def test_toggle_pref_drops_participant_when_last_preference_removed():
    """Once a player's prefs go empty they're cleanly removed from the lobby."""
    payload = {"participants": [42], "prefs": {"42": ["sfw_truth"]}}
    action = toggle_pref(payload, 42, "sfw_truth")
    assert action == "removed"
    assert payload["participants"] == []
    assert "42" not in payload["prefs"]


def test_toggle_pref_appends_second_category_for_same_user():
    payload: dict = {}
    toggle_pref(payload, 42, "sfw_truth")
    action = toggle_pref(payload, 42, "nsfw_dare")
    assert action == "added"
    assert payload["prefs"]["42"] == ["sfw_truth", "nsfw_dare"]
    # User listed only once in participants even after multiple prefs
    assert payload["participants"] == [42]


def test_toggle_pref_handles_multiple_users_independently():
    payload: dict = {}
    toggle_pref(payload, 1, "sfw_truth")
    toggle_pref(payload, 2, "nsfw_dare")
    assert payload["participants"] == [1, 2]
    assert payload["prefs"] == {"1": ["sfw_truth"], "2": ["nsfw_dare"]}


def test_toggle_pref_single_choice_replaces_existing_pick():
    """In single-choice mode a new pick swaps out the old one (radio-style)."""
    payload: dict = {}
    toggle_pref(payload, 42, "sfw_truth", single_choice=True)
    action = toggle_pref(payload, 42, "nsfw_dare", single_choice=True)
    assert action == "switched"
    assert payload["prefs"]["42"] == ["nsfw_dare"]
    # Still a single participant entry after the swap
    assert payload["participants"] == [42]


def test_toggle_pref_single_choice_first_pick_is_a_plain_add():
    """A player's first pick in single-choice mode is an ordinary add, not a swap."""
    payload: dict = {}
    action = toggle_pref(payload, 42, "sfw_dare", single_choice=True)
    assert action == "added"
    assert payload["prefs"]["42"] == ["sfw_dare"]


def test_toggle_pref_single_choice_deselects_to_empty():
    """Tapping the already-selected category in single-choice mode clears it."""
    payload: dict = {}
    toggle_pref(payload, 42, "sfw_truth", single_choice=True)
    action = toggle_pref(payload, 42, "sfw_truth", single_choice=True)
    assert action == "removed"
    assert payload["participants"] == []
    assert "42" not in payload["prefs"]


# ── record_asked ─────────────────────────────────────────────────────


def test_record_asked_writes_under_composite_key():
    payload: dict = {}
    record_asked(payload, target_id="42", category="sfw_truth", question="Q?")
    assert payload["asked"] == {"42:sfw_truth": "Q?"}


def test_record_asked_preserves_other_entries():
    payload = {"asked": {"1:sfw_truth": "Old"}}
    record_asked(payload, "2", "nsfw_dare", "New")
    assert payload["asked"] == {"1:sfw_truth": "Old", "2:nsfw_dare": "New"}


def test_record_asked_overwrites_same_pair():
    """A second question for the same (user, category) overwrites — this
    mirrors how the cog uses the key as a dedup guard."""
    payload: dict = {}
    record_asked(payload, "42", "sfw_truth", "First")
    record_asked(payload, "42", "sfw_truth", "Second")
    assert payload["asked"]["42:sfw_truth"] == "Second"


# ── available_targets ────────────────────────────────────────────────


def test_available_targets_returns_all_pairs_when_nothing_asked():
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare", "nsfw_truth"]}
    assert available_targets(prefs, {}) == [
        ("1", "sfw_truth"),
        ("2", "sfw_dare"),
        ("2", "nsfw_truth"),
    ]


def test_available_targets_filters_out_already_asked_pairs():
    prefs = {"1": ["sfw_truth", "sfw_dare"]}
    asked = {"1:sfw_truth": "Q"}
    assert available_targets(prefs, asked) == [("1", "sfw_dare")]


def test_available_targets_returns_empty_when_everything_asked():
    prefs = {"1": ["sfw_truth"]}
    asked = {"1:sfw_truth": "Q"}
    assert available_targets(prefs, asked) == []


# ── asked_counts_by_user ─────────────────────────────────────────────


def test_asked_counts_by_user_groups_by_user_id():
    asked = {
        "1:sfw_truth": "a",
        "1:sfw_dare": "b",
        "2:nsfw_dare": "c",
    }
    assert asked_counts_by_user(asked) == {"1": 2, "2": 1}


def test_asked_counts_by_user_empty():
    assert asked_counts_by_user({}) == {}


# ── select_next_question_target ──────────────────────────────────────


def test_select_next_question_target_returns_none_when_no_prefs():
    assert select_next_question_target({}, {}) is None


def test_select_next_question_target_returns_none_when_all_asked():
    prefs = {"1": ["sfw_truth"]}
    asked = {"1:sfw_truth": "Q"}
    assert select_next_question_target(prefs, asked) is None


def test_select_next_question_target_prefers_least_asked_user():
    """User 1 already has 2 questions; user 2 has 0. The picker MUST
    pick user 2 even though user 1 still has pending categories."""
    prefs = {
        "1": ["sfw_truth", "nsfw_truth"],
        "2": ["sfw_dare"],
    }
    asked = {"1:sfw_dare": "Q", "1:nsfw_dare": "Q"}
    rng = random.Random(0)
    result = select_next_question_target(prefs, asked, rng=rng)
    assert result == ("2", "sfw_dare")


def test_select_next_question_target_picks_uniformly_among_tied_users():
    """When two users are tied for least-asked, the rng decides — pin
    the seed so the test is deterministic but the algorithm is still
    exercised."""
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare"]}
    rng = random.Random(0)
    result = select_next_question_target(prefs, {}, rng=rng)
    assert result in {("1", "sfw_truth"), ("2", "sfw_dare")}


def test_select_next_question_target_uses_module_random_when_rng_omitted():
    """Default path: passing no rng falls back to the module-level random.
    We only assert it returns SOMETHING valid — the actual choice is
    random, but it must be one of the available pairs."""
    prefs = {"1": ["sfw_truth"]}
    result = select_next_question_target(prefs, {})
    assert result == ("1", "sfw_truth")


def test_select_next_question_target_recounts_after_each_call():
    """Picker is stateless; the cog keeps calling it and the asked-dict
    grows. Simulate two consecutive picks."""
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare"]}
    payload: dict = {}
    rng = random.Random(1)

    first = select_next_question_target(prefs, payload.get("asked", {}), rng=rng)
    assert first is not None
    uid, cat = first
    record_asked(payload, uid, cat, "Q")

    second = select_next_question_target(prefs, payload["asked"], rng=rng)
    assert second is not None
    # The second pick must NOT be the same player (other player has 0 asked)
    second_uid, _ = second
    assert second_uid != uid


# ── summarize_asked_by_category ──────────────────────────────────────


def test_summarize_asked_by_category_zero_when_nothing_asked():
    result = summarize_asked_by_category({})
    assert result == {cat: 0 for cat in CATEGORIES}


def test_summarize_asked_by_category_counts_each_category():
    asked = {
        "1:sfw_truth": "a",
        "2:sfw_truth": "b",
        "3:nsfw_dare": "c",
    }
    result = summarize_asked_by_category(asked)
    assert result["sfw_truth"] == 2
    assert result["nsfw_dare"] == 1
    assert result["sfw_dare"] == 0
    assert result["nsfw_truth"] == 0


def test_summarize_asked_by_category_tolerates_unknown_categories():
    """Stale payloads may carry categories that no longer exist in the
    cog's CATEGORIES list. They should still be counted, not crash."""
    asked = {"1:legacy_cat": "old question"}
    result = summarize_asked_by_category(asked)
    assert result["legacy_cat"] == 1
    # Known categories are still there at zero
    assert result["sfw_truth"] == 0


# ── question_pool_size ───────────────────────────────────────────────


def test_question_pool_size_sums_pref_combos():
    prefs = {"1": ["sfw_truth", "sfw_dare"], "2": ["nsfw_dare"]}
    assert question_pool_size(prefs, {}) == 3


def test_question_pool_size_empty():
    assert question_pool_size({}, {}) == 0


def test_question_pool_size_counts_asked_combos_dropped_from_prefs():
    """A category asked then removed from a player's prefs still counts,
    so the denominator never drops below the number already asked."""
    prefs = {"1": ["sfw_truth"]}  # player dropped sfw_dare after being asked
    asked = {"1:sfw_truth": "Q", "1:sfw_dare": "Q"}
    assert question_pool_size(prefs, asked) == 2


# ── build_tod_embed ──────────────────────────────────────────────────


def test_build_tod_embed_shows_zero_counts_for_empty_payload():
    embed = build_tod_embed("Alice", {})
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert by_name["Participants"] == "0"
    assert by_name["Questions Asked"] == "0"
    assert embed.title is not None
    assert "TRUTH OR DARE" in embed.title
    assert "GAME OVER" not in embed.title


def test_build_tod_embed_counts_participants_and_asked():
    payload = {
        "participants": [1, 2, 3],
        "asked": {"1:sfw_truth": "Q", "2:sfw_dare": "Q"},
    }
    embed = build_tod_embed("Alice", payload)
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Participants"] == "3"
    # No prefs in the payload, so the pool is just the asked combos: 2 / 2.
    assert by_name["Questions Asked"] == "2 / 2"


def test_build_tod_embed_shows_progress_against_pref_pool():
    """The 'Questions Asked' field reports X / Y against the full pool of
    declared (player, category) combinations."""
    payload = {
        "participants": [1, 2],
        "prefs": {"1": ["sfw_truth", "sfw_dare"], "2": ["nsfw_dare"]},
        "asked": {"1:sfw_truth": "Q"},
    }
    embed = build_tod_embed("Alice", payload)
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Questions Asked"] == "1 / 3"


def test_build_tod_embed_closed_flag_changes_title():
    embed = build_tod_embed("Alice", {}, closed=True)
    assert embed.title is not None
    assert "GAME OVER" in embed.title


def test_build_tod_embed_has_footer():
    embed = build_tod_embed("Alice", {})
    assert embed.footer.text is not None
    assert "Truth or Dare" in embed.footer.text


def test_build_tod_embed_footer_flags_single_choice():
    plain = build_tod_embed("Alice", {})
    single = build_tod_embed("Alice", {"single_choice": True})
    assert "One category each" not in (plain.footer.text or "")
    assert "One category each" in (single.footer.text or "")


def test_build_lobby_embed_single_choice_changes_prompt_and_footer():
    plain = build_lobby_embed("Alice")
    single = build_lobby_embed("Alice", single_choice=True)
    assert "as many" not in (plain.description or "")  # default prompt unchanged
    assert "one category" in (single.description or "").lower()
    assert "One category each" in (single.footer.text or "")


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_shows_totals():
    payload = {
        "participants": [1, 2, 3],
        "asked": {"1:sfw_truth": "Q", "2:sfw_dare": "Q"},
    }
    embed = build_recap_embed(payload)
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Total Questions Asked"] == "2"
    assert by_name["Participants"] == "3"
    assert embed.title is not None
    assert "GAME OVER" in embed.title


def test_build_recap_embed_only_shows_nonzero_category_breakdowns():
    payload = {
        "participants": [1, 2],
        "asked": {"1:sfw_truth": "Q", "2:sfw_truth": "Q"},
    }
    embed = build_recap_embed(payload)
    by_name = {f.name: f.value for f in embed.fields}
    assert "SFW Truth" in by_name
    assert by_name["SFW Truth"] == "2"
    # Empty categories must NOT show up — keeps the recap tidy
    assert "SFW Dare" not in by_name
    assert "NSFW Truth" not in by_name


def test_build_recap_embed_with_zero_asked():
    embed = build_recap_embed({"participants": [], "asked": {}})
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Total Questions Asked"] == "0"
    assert by_name["Participants"] == "0"


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_has_join_prompt_in_description():
    embed = build_lobby_embed("Alice")
    assert embed.description is not None
    assert "preferences" in embed.description.lower()


def test_build_lobby_embed_shows_host_name():
    embed = build_lobby_embed("Alice")
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert by_name["Participants"] == "0"
    assert by_name["Questions Asked"] == "0"


# ── build_question_embed ─────────────────────────────────────────────


def test_build_question_embed_includes_category_label_and_question():
    embed = build_question_embed("sfw_truth", "What's your favorite color?", "Alice")
    assert "SFW TRUTH" in embed.title
    assert "What's your favorite color?" in embed.description
    assert embed.author.name == "For Alice"


def test_build_question_embed_uses_category_label_lookup():
    """Known categories render as their friendly label, not the raw key."""
    embed = build_question_embed("nsfw_dare", "Q?")
    assert CAT_LABELS["nsfw_dare"].upper() in embed.title
    assert "nsfw_dare" not in embed.title


def test_build_question_embed_distinct_color_per_category():
    """Each category carries its own accent color so cards differ at a glance."""
    colors = {
        cat: build_question_embed(cat, "Q?").color.value for cat in CATEGORIES
    }
    assert len(set(colors.values())) == len(CATEGORIES)


def test_build_question_embed_preserves_markdown():
    """The question's markdown is left intact so the embed renders it."""
    embed = build_question_embed("sfw_truth", "*emphasised*")
    assert "*emphasised*" in embed.description


def test_build_question_embed_keeps_multiline_inside_blockquote():
    embed = build_question_embed("sfw_truth", "line one\nline two")
    assert "> line one" in embed.description
    assert "> line two" in embed.description


def test_build_question_embed_falls_back_when_category_unknown():
    """Unknown category keys (stale payloads) shouldn't crash — they fall
    back to a neutral style and use the raw key as the label."""
    embed = build_question_embed("legacy_cat", "Q?")
    assert "LEGACY_CAT" in embed.title


def test_build_question_embed_omits_author_without_target():
    embed = build_question_embed("sfw_dare", "Q?")
    assert embed.author.name is None


# ── CATEGORIES / CAT_LABELS sanity ──────────────────────────────────


def test_categories_and_labels_stay_aligned():
    """Every category constant has a friendly label and vice versa."""
    assert set(CATEGORIES) == set(CAT_LABELS.keys())


@pytest.mark.parametrize("cat", CATEGORIES)
def test_each_category_has_a_human_label(cat):
    assert CAT_LABELS[cat]


# ── select_bank_categories_for_all ───────────────────────────────────


def test_bank_categories_picks_one_per_participant():
    prefs = {"1": ["sfw_truth", "sfw_dare"], "2": ["nsfw_dare"]}
    chosen = select_bank_categories_for_all(prefs, {}, rng=random.Random(0))
    assert set(chosen.keys()) == {"1", "2"}
    assert chosen["1"] in prefs["1"]
    assert chosen["2"] == "nsfw_dare"


def test_bank_categories_omits_players_with_no_prefs():
    prefs = {"1": ["sfw_truth"], "2": []}
    chosen = select_bank_categories_for_all(prefs, {})
    assert set(chosen.keys()) == {"1"}


def test_bank_categories_empty_prefs_gives_empty():
    assert select_bank_categories_for_all({}, {}) == {}


def test_bank_categories_skips_already_asked_pairs():
    # Player 1 was already asked sfw_truth (bank or written — same history),
    # so only sfw_dare remains for them; player 2 is fully asked and omitted.
    prefs = {"1": ["sfw_truth", "sfw_dare"], "2": ["nsfw_dare"]}
    asked = {"1:sfw_truth": "q1", "2:nsfw_dare": "q2"}
    chosen = select_bank_categories_for_all(prefs, asked, rng=random.Random(0))
    assert chosen == {"1": "sfw_dare"}


def test_bank_categories_empty_when_everyone_fully_asked():
    prefs = {"1": ["sfw_truth"]}
    asked = {"1:sfw_truth": "q1"}
    assert select_bank_categories_for_all(prefs, asked) == {}


# ── get_traditional_question (bank getter) ───────────────────────────


class _FakeDB:
    """Minimal async db exposing only fetchall(query, params) for the getter."""

    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self, query, params=()):
        return list(self._rows)


def _row(text, category):
    import json
    return (text, json.dumps([category]))


async def test_get_traditional_question_exact_category_match():
    db = _FakeDB([_row("truth Q", "sfw_truth"), _row("dare Q", "sfw_dare")])
    got = await get_traditional_question(db, "sfw_truth")
    assert got == "truth Q"


async def test_get_traditional_question_nsfw_is_a_distinct_category():
    # An sfw category must never serve an nsfw-tagged question.
    db = _FakeDB([_row("spicy", "nsfw_truth")])
    assert await get_traditional_question(db, "sfw_truth") is None
    assert await get_traditional_question(db, "nsfw_truth") == "spicy"


async def test_get_traditional_question_excludes_used():
    db = _FakeDB([_row("a", "sfw_dare"), _row("b", "sfw_dare")])
    got = await get_traditional_question(db, "sfw_dare", exclude=["a"])
    assert got == "b"


async def test_get_traditional_question_none_when_empty():
    assert await get_traditional_question(_FakeDB([]), "sfw_truth") is None


async def test_get_traditional_question_unknown_category_returns_none():
    db = _FakeDB([_row("a", "sfw_truth")])
    assert await get_traditional_question(db, "bogus") is None
