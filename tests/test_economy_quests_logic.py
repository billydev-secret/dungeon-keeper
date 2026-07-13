"""Tests for bot_modules/economy/quests.py — pure quest math.

The ISO-week period key (including the year-rollover boundary), the library
slot matrix, the rotate-pool cursor, and the reward bands are all table-driven
since they gate money-critical behavior in the service layer.
"""

from __future__ import annotations

import pytest

from bot_modules.economy.quests import (
    can_activate,
    compile_trigger_pattern,
    iso_week_for,
    message_matches_trigger,
    parse_trigger_words,
    photo_card_period,
    pick_rotation,
    quest_period,
    reward_band,
)


# ── iso_week_for / year rollover ──────────────────────────────────────


@pytest.mark.parametrize(
    "local_day,expected",
    [
        ("2026-07-10", "2026-W28"),
        ("2023-01-02", "2023-W01"),
        # ISO year != calendar year at the boundaries:
        ("2024-12-30", "2025-W01"),  # Monday belongs to next ISO year
        ("2024-12-31", "2025-W01"),
        ("2023-01-01", "2022-W52"),  # Sunday belongs to prior ISO year
        # A 53-week ISO year: Jan 1 falls in the prior year's W53:
        ("2021-01-01", "2020-W53"),
        ("2021-01-04", "2021-W01"),  # first Monday of the 2021 ISO year
    ],
)
def test_iso_week_for(local_day, expected):
    assert iso_week_for(local_day) == expected


def test_iso_week_zero_padded():
    # Single-digit weeks pad to two digits so the string sorts lexically.
    assert iso_week_for("2026-01-05") == "2026-W02"


# ── quest_period ──────────────────────────────────────────────────────


def test_quest_period_daily_is_the_day():
    assert quest_period("daily", "2026-07-10") == "2026-07-10"


def test_quest_period_weekly_is_iso_week():
    assert quest_period("weekly", "2026-07-10") == "2026-W28"


def test_quest_period_community_is_once():
    assert quest_period("community", "2026-07-10") == "once"


def test_quest_period_unknown_raises():
    with pytest.raises(ValueError):
        quest_period("monthly", "2026-07-10")


def test_quest_period_event_raises():
    # Event quests have no calendar period — the listener supplies a
    # per-occurrence key, so a calendar lookup is a bug.
    with pytest.raises(ValueError):
        quest_period("event", "2026-07-10")


def test_photo_card_period_is_keyed_to_the_card():
    assert photo_card_period("abc-123") == "photo:abc-123"


# ── can_activate: slot matrix ─────────────────────────────────────────


@pytest.mark.parametrize(
    "existing,qtype,expected",
    [
        # daily: at most one active
        ([], "daily", True),
        (["weekly", "weekly"], "daily", True),
        (["daily"], "daily", False),
        # weekly: up to five active
        ([], "weekly", True),
        (["weekly"] * 4, "weekly", True),
        (["weekly"] * 5, "weekly", False),
        (["weekly"] * 5 + ["daily"], "weekly", False),
        # a daily active does not eat a weekly slot
        (["daily"], "weekly", True),
        # community: uncapped
        (["community"] * 20, "community", True),
        ([], "community", True),
        # event: at most one active (the listener would double-pay on two)
        ([], "event", True),
        (["daily", "weekly"], "event", True),
        (["event"], "event", False),
    ],
)
def test_can_activate(existing, qtype, expected):
    assert can_activate(existing, qtype) is expected


def test_can_activate_unknown_raises():
    with pytest.raises(ValueError):
        can_activate([], "monthly")


# ── pick_rotation: cursor cycling ─────────────────────────────────────


@pytest.mark.parametrize(
    "pool,current,expected",
    [
        ([], None, None),
        ([7], 7, None),  # pool of one has nowhere to rotate
        ([7], None, None),
        ([1, 2, 3], 1, 2),
        ([1, 2, 3], 2, 3),
        ([1, 2, 3], 3, 1),  # wraps around
        ([1, 2, 3], None, 1),  # no current -> first
        ([1, 2, 3], 99, 1),  # current not in pool -> first
        ([3, 1, 2], 2, 3),  # unordered input sorts by id
        ([1, 1, 2], 1, 2),  # duplicates collapse
    ],
)
def test_pick_rotation(pool, current, expected):
    assert pick_rotation(pool, current) == expected


def test_pick_rotation_full_cycle():
    pool = [10, 20, 30]
    seen = []
    cur = None
    for _ in range(6):
        cur = pick_rotation(pool, cur)
        seen.append(cur)
    assert seen == [10, 20, 30, 10, 20, 30]


# ── reward_band ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "qtype,expected",
    [
        ("daily", (10, 20)),
        ("weekly", (25, 75)),
        ("community", None),
        ("monthly", None),
    ],
)
def test_reward_band(qtype, expected):
    assert reward_band(qtype) == expected


# ── trigger phrases: parse ────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gm, good morning", ["gm", "good morning"]),
        ("one\ntwo", ["one", "two"]),
        ("", []),
        (" ,  , \n ", []),
        ("GM, gm", ["GM"]),  # case-insensitive dedupe keeps first spelling
        ("good   morning", ["good morning"]),  # internal whitespace collapses
        ("a,\nb, a", ["a", "b"]),
    ],
)
def test_parse_trigger_words(raw, expected):
    assert parse_trigger_words(raw) == expected


# ── trigger phrases: match ────────────────────────────────────────────


@pytest.mark.parametrize(
    "content,matches",
    [
        ("gm everyone", True),
        ("GM!", True),  # case-insensitive, punctuation boundary
        ("well, gm", True),
        ("Good  Morning y'all", True),  # phrase spans a whitespace run
        ("dogma", False),  # no match inside a word
        ("gmail is down", False),
        ("goodmorning", False),  # phrase needs its internal gap
        ("", False),
    ],
)
def test_message_matches_trigger(content, matches):
    pattern = compile_trigger_pattern(["gm", "good morning"])
    assert message_matches_trigger(content, pattern) is matches


def test_trigger_pattern_nonword_phrase_anchors():
    # Phrases wrapped in non-word chars still bound on their neighbors.
    pattern = compile_trigger_pattern([":wave:"])
    assert message_matches_trigger("hello :wave: there", pattern)
    assert not message_matches_trigger("a:wave:b", pattern)


def test_trigger_pattern_escapes_regex_metachars():
    pattern = compile_trigger_pattern(["what?!"])
    assert message_matches_trigger("ok what?! wild", pattern)
    assert not message_matches_trigger("ok what wild", pattern)


def test_trigger_pattern_empty_is_none():
    assert compile_trigger_pattern([]) is None
    assert message_matches_trigger("anything", None) is False
