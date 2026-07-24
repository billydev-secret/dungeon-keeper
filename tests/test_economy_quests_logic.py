"""Tests for bot_modules/economy/quests.py — pure quest math.

The ISO-week period key (including the year-rollover boundary), the library
slot matrix, the rotate-pool cursor, and the reward bands are all table-driven
since they gate money-critical behavior in the service layer.
"""

from __future__ import annotations

import pytest

from bot_modules.economy.quests import (
    POOL_CAP,
    PERSONAL_BOARD_SIZE,
    assigned_quest_ids,
    board_size,
    can_activate,
    can_activate_event,
    community_auto_target,
    compile_trigger_pattern,
    effective_target,
    has_board,
    iso_week_for,
    apply_pair_bundles,
    message_matches_trigger,
    occurrence_period,
    p25_target,
    pair_map,
    parse_trigger_words,
    period_index,
    pick_rotation,
    previous_local_day,
    quest_period,
    reward_band,
)


# ── previous_local_day ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-07-23", "2026-07-22"),
        ("2026-07-01", "2026-06-30"),  # month boundary
        ("2026-01-01", "2025-12-31"),  # year boundary
        ("2026-03-01", "2026-02-28"),  # non-leap February
        ("2024-03-01", "2024-02-29"),  # leap February
    ],
)
def test_previous_local_day(day, expected):
    assert previous_local_day(day) == expected


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


def test_quest_period_monthly_is_calendar_month():
    # Plain calendar months: the window opens on the 1st, guild-local —
    # no ISO-style shifting at year/month boundaries.
    assert quest_period("monthly", "2026-07-01") == "2026-07"
    assert quest_period("monthly", "2026-07-31") == "2026-07"
    assert quest_period("monthly", "2026-08-01") == "2026-08"
    assert quest_period("monthly", "2026-12-31") == "2026-12"
    assert quest_period("monthly", "2027-01-01") == "2027-01"


def test_quest_period_unknown_raises():
    with pytest.raises(ValueError):
        quest_period("yearly", "2026-07-10")


def test_quest_period_event_raises():
    # Event quests have no calendar period — the listener supplies a
    # per-occurrence key, so a calendar lookup is a bug.
    with pytest.raises(ValueError):
        quest_period("event", "2026-07-10")


def test_occurrence_period_is_keyed_to_the_occurrence():
    assert occurrence_period("photo_post", "abc-123") == "photo_post:abc-123"
    assert occurrence_period("duel", "quickdraw:5") == "duel:quickdraw:5"


# ── can_activate: slot matrix ─────────────────────────────────────────


@pytest.mark.parametrize(
    "existing,qtype,expected",
    [
        # daily/weekly each form a pool capped at POOL_CAP; the per-user board
        # draws N of them, so many can be active at once.
        ([], "daily", True),
        (["weekly", "weekly"], "daily", True),
        (["daily"] * 2, "daily", True),
        (["daily"] * POOL_CAP, "daily", False),
        # weekly: own pool, capped at POOL_CAP
        ([], "weekly", True),
        (["weekly"] * (POOL_CAP - 1), "weekly", True),
        (["weekly"] * POOL_CAP, "weekly", False),
        (["weekly"] * POOL_CAP + ["daily"], "weekly", False),
        # a daily active does not eat a weekly slot
        (["daily"], "weekly", True),
        # monthly: guild-wide community goal now — uncapped (its rotation owns
        # the single active lane), like community.
        ([], "monthly", True),
        (["monthly"] * POOL_CAP, "monthly", True),
        # community: uncapped
        (["community"] * 20, "community", True),
        ([], "community", True),
        # event: uncapped at the type level (per-kind cap is separate)
        ([], "event", True),
        (["event", "event"], "event", True),
    ],
)
def test_can_activate(existing, qtype, expected):
    assert can_activate(existing, qtype) is expected


@pytest.mark.parametrize(
    "existing_kinds,kind,expected",
    [
        ([], "photo_post", True),
        (["duel", "party_game"], "photo_post", True),  # other kinds don't block
        (["photo_post"], "photo_post", False),  # one active per kind
    ],
)
def test_can_activate_event(existing_kinds, kind, expected):
    assert can_activate_event(existing_kinds, kind) is expected


def test_can_activate_unknown_raises():
    with pytest.raises(ValueError):
        can_activate([], "yearly")


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
        ("monthly", (50, 90)),
        ("yearly", None),
    ],
)
def test_reward_band(qtype, expected):
    assert reward_band(qtype) == expected


# ── community goal sizing: cadence-aware divisor ──────────────────────


def test_community_auto_target_weekly_default_unchanged():
    # 4 trailing weeks × 75/week = 300 → typical 75 → 75/0.75 = 100.
    assert community_auto_target(300) == 100
    assert community_auto_target(300, periods_in_window=4.0) == 100


def test_community_auto_target_monthly_divisor_is_a_full_window():
    # A monthly goal spans the whole 28-day window (periods_in_window=1), so
    # the same total sizes ~4× higher than the weekly reading.
    assert community_auto_target(300, periods_in_window=1.0) == 400
    assert community_auto_target(300, periods_in_window=1.0) == 4 * community_auto_target(300)


def test_community_auto_target_floor_holds_for_both_cadences():
    assert community_auto_target(0) == 10
    assert community_auto_target(0, periods_in_window=1.0) == 10
    assert community_auto_target(3, periods_in_window=1.0) == 10  # 4/0.75≈5 < floor


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


# ── period_index: monotonic per cadence ───────────────────────────────


def test_period_index_daily_advances_by_one_per_day():
    assert period_index("daily", "2026-07-14") - period_index("daily", "2026-07-13") == 1


def test_period_index_weekly_stable_within_week_steps_between():
    # Mon..Sun of one ISO week share an index; the next week is higher.
    mon = period_index("weekly", "2026-07-13")
    sun = period_index("weekly", "2026-07-19")
    nxt = period_index("weekly", "2026-07-20")
    assert mon == sun
    assert nxt > mon


def test_period_index_monthly_stable_within_month():
    assert period_index("monthly", "2026-07-01") == period_index("monthly", "2026-07-31")
    assert period_index("monthly", "2026-08-01") - period_index("monthly", "2026-07-01") == 1


@pytest.mark.parametrize("qtype", ["community", "event", "yearly"])
def test_period_index_no_calendar_raises(qtype):
    with pytest.raises(ValueError):
        period_index(qtype, "2026-07-13")


# ── assigned_quest_ids: per-user board draw ───────────────────────────


def test_assigned_size_and_membership():
    pool = [10, 20, 30, 40, 50]
    got = assigned_quest_ids(pool, user_id=1, index=0, n=2)
    assert len(got) == 2
    assert set(got) <= set(pool)


def test_assigned_is_deterministic():
    pool = [1, 2, 3, 4, 5, 6]
    a = assigned_quest_ids(pool, user_id=7, index=3, n=2)
    b = assigned_quest_ids(pool, user_id=7, index=3, n=2)
    assert a == b


def test_assigned_differs_across_users():
    pool = list(range(1, 13))
    sets = {tuple(assigned_quest_ids(pool, u, index=0, n=2)) for u in range(30)}
    # Not everyone gets the identical pair.
    assert len(sets) > 1


def test_assigned_no_repeat_until_pool_cycled():
    # Walking consecutive periods covers the whole pool before any id recurs.
    pool = list(range(1, 11))  # 10 quests, n=2 -> 5 periods to cover all
    seen: list[int] = []
    for idx in range(5):
        seen.extend(assigned_quest_ids(pool, user_id=99, index=idx, n=2))
    assert sorted(seen) == pool  # each id exactly once across the 5-period cycle


def test_assigned_n_ge_pool_returns_all():
    pool = [3, 1, 2]
    assert assigned_quest_ids(pool, user_id=5, index=0, n=5) == [1, 2, 3]


def test_assigned_empty_pool():
    assert assigned_quest_ids([], user_id=1, index=0, n=2) == []


# ── board_size / has_board: the configurable dial ─────────────────────


def test_board_size_defaults():
    assert board_size("daily") == PERSONAL_BOARD_SIZE["daily"]
    assert board_size("community") == 0


def test_board_size_override_wins():
    sizes = {"daily": 5, "weekly": 0}
    assert board_size("daily", sizes) == 5
    # 0 is a real value, not "unset" — it must not fall back to the default.
    assert board_size("weekly", sizes) == 0
    # A cadence absent from the override keeps its default.
    assert board_size("weekly", {"daily": 5}) == PERSONAL_BOARD_SIZE["weekly"]
    # Monthly is a guild-wide goal now, not a board cadence → no board draw.
    assert board_size("monthly", sizes) == 0


def test_has_board_is_independent_of_size():
    # The predicate that keeps "sized to 0" from being read as "no board, so
    # every active quest counts" — the two board cadences always have one.
    for qtype in ("daily", "weekly"):
        # Still has a board when the guild sized it to 0 — it's just empty.
        assert has_board(qtype)
        assert board_size(qtype, {qtype: 0}) == 0
    # Monthly and community are guild-wide, no personal board.
    assert not has_board("monthly")
    assert not has_board("community")
    assert not has_board("community")
    assert not has_board("event")


# ── effective_target: gaussian band, deterministic ────────────────────


def test_effective_target_no_band_is_fixed():
    assert effective_target(7, 0, 0, user_id=1, quest_id=1, period="2026-07-13") == 7


def test_effective_target_within_band_and_deterministic():
    vals = {
        effective_target(0, 5, 20, user_id=u, quest_id=1, period="2026-W28")
        for u in range(200)
    }
    assert all(5 <= v <= 20 for v in vals)
    assert len(vals) > 3  # the draw actually varies across members
    # stable for a given (user, quest, period)
    a = effective_target(0, 5, 20, user_id=42, quest_id=1, period="2026-W28")
    b = effective_target(0, 5, 20, user_id=42, quest_id=1, period="2026-W28")
    assert a == b


def test_effective_target_never_below_one():
    assert effective_target(0, 0, 0, user_id=1, quest_id=1, period="p") == 1


def test_effective_target_cold_start_anchors_below_midpoint():
    # Regression: the cold-start draw stands in for a member too new/quiet to
    # size from their own pace — the warm path would clamp them toward the
    # floor, so centering on the band MIDPOINT made the fallback harder than
    # what it replaces. The distribution's mean must sit below the midpoint.
    lo, hi = 5, 20
    midpoint = (lo + hi) / 2
    vals = [
        effective_target(0, lo, hi, user_id=u, quest_id=1, period="2026-W28")
        for u in range(400)
    ]
    assert all(lo <= v <= hi for v in vals)
    mean = sum(vals) / len(vals)
    assert mean < midpoint  # anchored low, not centred


# ── p25_target: personal p25, band-clamped ────────────────────────────


def test_p25_target_takes_quartile_not_median():
    # sorted 20/20/30/40 → p25 = 20 (the median×1.15 path would give 29)
    assert p25_target([20, 20, 30, 40], 4, 40) == 20


def test_p25_target_zeros_drag_it_to_the_band_floor():
    # two quiet trailing weeks pull p25 to 0 → floored at band min
    assert p25_target([0, 0, 1, 2], 4, 40) == 4


def test_p25_target_counts_partial_history():
    # one active week among four: p25 = 7.5 → 8 (round-half-even)
    assert p25_target([0, 30, 60, 90], 4, 40) == 8


def test_p25_target_clamps_to_band_max():
    assert p25_target([100, 100, 100, 100], 4, 40) == 40


def test_p25_target_degenerate_inputs():
    assert p25_target([3], 1, 40) == 3  # single period: use it as-is
    assert p25_target([], 1, 40) == 1  # no history: band floor / never 0


# ── paired board quests ───────────────────────────────────────────────


def test_pair_map_exact_twos_only():
    tagged = {1: "gw", 2: "gw", 3: "", 4: "wh", 5: "wh", 6: "wh", 7: "solo"}
    pairs = pair_map(tagged)
    assert pairs == {1: 2, 2: 1}  # 'wh' ×3 and 'solo' ×1 are inert


def test_apply_pair_bundles_pulls_partner_in():
    # drew {2, 9}; 2 pairs with 5 → 9 gives way to the partner
    assert apply_pair_bundles([2, 9], {2: 5, 5: 2}) == [2, 5]


def test_apply_pair_bundles_pair_already_complete():
    assert apply_pair_bundles([2, 5], {2: 5, 5: 2}) == [2, 5]


def test_apply_pair_bundles_no_tagged_quests_is_identity():
    assert apply_pair_bundles([3, 7], {2: 5, 5: 2}) == [3, 7]


def test_apply_pair_bundles_board_of_one_cannot_pair():
    assert apply_pair_bundles([2], {2: 5, 5: 2}) == [2]


def test_apply_pair_bundles_two_pairs_first_wins():
    # Both drawn quests belong to different pairs on a 2-slot board: the
    # lower id completes its pair; the other is displaced. Deterministic.
    assert apply_pair_bundles([2, 8], {2: 5, 5: 2, 8: 9, 9: 8}) == [2, 5]


def test_apply_pair_bundles_wider_board_keeps_unpaired():
    # Board of 3: pair completes, the untagged quest keeps its slot.
    assert apply_pair_bundles([2, 3, 9], {2: 5, 5: 2}) == [2, 3, 5]


def test_apply_pair_bundles_never_splits_a_complete_pair():
    # 8+9 arrived complete; 2 completing its own pair must displace the
    # loose quest (3), not a member of the intact pair.
    got = apply_pair_bundles([2, 3, 8, 9], {2: 5, 5: 2, 8: 9, 9: 8})
    assert got == [2, 5, 8, 9]
