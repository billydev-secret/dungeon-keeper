"""Tests for the candidate generator and card selector — stage 3 of
docs/plans/mahjong-card-generator.md.

The invariant that matters most is the first one: **the generator can never
emit a hand the engine cannot play**, because every candidate goes through
the real linter before it may enter the pool. Everything else here is about
the card the selector builds out of that pool being one a table could
actually enjoy — sections that keep their promises, no line printed twice,
and no line stranded without a pivot.

The pool is built once per module (a fresh enumeration is ~0.4s).
"""

from __future__ import annotations

from collections import Counter

import pytest

from bot_modules.games.mahjong.card_gen import (
    MAX_OVERLAP,
    S_PAIRS,
    SECTIONS,
    build_card,
    candidates,
    overlap,
    pivot_report,
    provisional_value,
    select,
    stutter_key,
)
from bot_modules.games.mahjong.card_logic import (
    HAND_TILES,
    VALUE_MAX,
    VALUE_MIN,
    lint_card_data,
    load_card,
)


@pytest.fixture(scope="module")
def pool():
    return candidates(year="2026")


@pytest.fixture(scope="module")
def generated(pool):
    data = build_card(
        select(pool, per_section=7, seed=1),
        card_id="gen-test", display_name="Generated", season="2026-test",
    )
    return data, load_card(data)


# ── The pool ─────────────────────────────────────────────────────────────────


def test_the_pool_is_not_empty_and_covers_every_section(pool):
    assert len(pool) > 200
    assert {c.section for c in pool} == set(SECTIONS)


def test_every_candidate_lints_clean(pool):
    """The load-bearing invariant. A single bad candidate means the
    generator can put an unplayable line on a card."""
    bad = []
    for c in pool:
        report = lint_card_data({
            "card_id": "x", "display_name": "x", "season": "x",
            "hands": [{
                "id": c.hand.id, "section": c.hand.section,
                "name": c.hand.name, "concealed": c.hand.concealed,
                "value": c.hand.value,
                "groups": [
                    {"count": g.count, "rank": g.rank}
                    | ({"suit": g.suit} if g.suit else {})
                    for g in c.hand.groups
                ],
            }],
        })
        if not report.ok:
            bad.append((c.hand.id, report.errors))
    assert bad == []


def test_every_candidate_is_exactly_fourteen_tiles(pool):
    assert all(c.hand.tile_total == HAND_TILES for c in pool)


def test_candidates_are_deduped_by_shape(pool):
    shapes = [c.shape for c in pool]
    assert len(shapes) == len(set(shapes))


def test_the_pool_is_deterministic():
    a = candidates(year="2026")
    b = candidates(year="2026")
    assert [c.shape for c in a] == [c.shape for c in b]


def test_the_year_parameter_reaches_the_year_section():
    tokens = {
        g.rank
        for c in candidates(year="2027") if c.section == "Year"
        for g in c.hand.groups
    }
    assert "7" in tokens and "6" not in tokens


# ── The selected card ────────────────────────────────────────────────────────


def test_the_generated_card_lints_clean(generated):
    data, _ = generated
    report = lint_card_data(data)
    assert report.errors == []
    assert report.warnings == []


def test_selection_respects_the_per_section_cap(pool):
    hands = select(pool, per_section=3, seed=2)
    counts = {}
    for h in hands:
        counts[h.section] = counts.get(h.section, 0) + 1
    assert counts and max(counts.values()) <= 3


def test_selection_is_deterministic_for_a_seed(pool):
    a = build_card(select(pool, per_section=4, seed=3),
                   card_id="a", display_name="a", season="s")
    b = build_card(select(pool, per_section=4, seed=3),
                   card_id="a", display_name="a", season="s")
    assert a == b


def test_hand_ids_are_unique_and_section_slugged(generated):
    data, card = generated
    ids = [h["id"] for h in data["hands"]]
    assert len(ids) == len(set(ids))
    assert all("-" in i for i in ids)


def test_no_two_lines_are_near_clones(generated):
    _, card = generated
    for i, a in enumerate(card.hands):
        for b in card.hands[i + 1:]:
            assert overlap(a, b) < MAX_OVERLAP, f"{a.id} and {b.id} are clones"


def test_no_section_prints_the_same_line_with_two_tails(generated):
    _, card = generated
    seen: dict[tuple[str, tuple], str] = {}
    for hand in card.hands:
        key = (hand.section, stutter_key(hand))
        assert key not in seen, f"{hand.id} restates {seen.get(key)}"
        seen[key] = hand.id


def test_almost_every_line_has_somewhere_to_pivot(generated):
    """A stranded line is a trap, so the selector repairs them — but it
    cannot always, and a card that quietly pretends otherwise would be
    worse than one that reports it. The bar is 'nearly all'."""
    _, card = generated
    stranded = [k for k, v in pivot_report(card).items() if v < 2]
    assert len(stranded) <= 2, stranded


def test_singles_and_pairs_really_is_singles_and_pairs(generated):
    _, card = generated
    lines = [h for h in card.hands if h.section == S_PAIRS]
    assert lines
    for hand in lines:
        assert all(g.count <= 2 for g in hand.groups), hand.display
        # a line with no callable group must be concealed — the linter
        # enforces it, so this also proves the section can never drift
        assert hand.concealed


def test_values_stay_inside_the_card_range(generated):
    _, card = generated
    assert all(VALUE_MIN <= h.value <= VALUE_MAX for h in card.hands)
    assert all(h.value % 5 == 0 for h in card.hands)


# ── Regressions from the stage-3 review ──────────────────────────────────────


def test_a_matched_dragon_on_an_unused_letter_is_normalised_away(pool):
    """`4(D)a` beside no other `a` group constrains nothing — it is `4(D)`.

    Both spellings in the pool meant the same hand could be selected twice,
    which neither the shape signature nor the near-duplicate warning can see.
    """
    for c in pool:
        letters = Counter(
            g.suit for g in c.hand.groups if g.suit is not None
        )
        for g in c.hand.groups:
            if g.rank == "D" and g.suit is not None:
                assert letters[g.suit] > 1, (
                    f"{c.hand.display} carries a suit letter that binds nothing"
                )


def test_no_candidate_prints_one_tile_as_two_multi_groups(pool):
    """`3(2)a … 3(2)a` demands six copies of a four-copy tile and reads as a
    printing error; a card would print one bigger group instead."""
    for c in pool:
        seen: dict[tuple[str, str], int] = {}
        for g in c.hand.groups:
            key = (g.rank, g.suit or "")
            if key in seen and (seen[key] > 1 or g.count > 1):
                pytest.fail(f"{c.hand.display} splits one tile across groups")
            seen[key] = max(seen.get(key, 0), g.count)


def test_repeated_singles_are_still_allowed(pool):
    """The exception to the rule above: writing the year out twice is how a
    year hand is spelled, and four singles of one tile is its whole supply."""
    doubled = [
        c for c in pool
        if sum(1 for g in c.hand.groups if g.count == 1 and g.rank == "2") > 1
    ]
    assert doubled, "the year-as-singles family should survive"


def test_the_year_written_once_survives_padding(pool):
    """A four-tile core needs a ten-tile tail; without one the whole family
    was generated and silently discarded."""
    singles = [
        c for c in pool
        if c.section == "Year"
        and sum(1 for g in c.hand.groups if g.count == 1) == 4
    ]
    assert singles


def test_section_slugs_stay_alphanumeric(generated):
    """Ids reach members through the reveal embed, so 'Winds & Dragons'
    must not become 'w&d-1'."""
    data, _ = generated
    for hand in data["hands"]:
        slug = hand["id"].rsplit("-", 1)[0]
        assert slug.isalnum(), hand["id"]


def test_pairs_runs_come_in_more_than_one_length(pool):
    """Both loop arms used to clamp to five, so only one run length existed."""
    lengths = {
        sum(1 for g in c.hand.groups if g.rank.startswith("x"))
        for c in pool if c.section == S_PAIRS
    }
    assert len(lengths - {0}) > 1, lengths


# ── The metrics themselves ───────────────────────────────────────────────────


def _hand(groups, section="S", concealed=False):
    return load_card({
        "card_id": "x", "display_name": "x", "season": "x",
        "hands": [{"id": "h", "section": section, "name": "n",
                   "concealed": concealed, "value": 25, "groups": groups}],
    }).hands[0]


def test_overlap_counts_shared_tiles_not_shared_groups():
    a = _hand([{"count": 4, "rank": "2", "suit": "a"},
               {"count": 4, "rank": "4", "suit": "a"},
               {"count": 4, "rank": "6", "suit": "a"},
               {"count": 2, "rank": "F"}])
    b = _hand([{"count": 4, "rank": "2", "suit": "a"},
               {"count": 4, "rank": "4", "suit": "a"},
               {"count": 3, "rank": "8", "suit": "a"},
               {"count": 3, "rank": "F"}])
    assert overlap(a, b) == 4 + 4 + 2  # the 2s, the 4s, and two flowers


def test_overlap_is_suit_letter_sensitive():
    a = _hand([{"count": 4, "rank": "2", "suit": "a"},
               {"count": 4, "rank": "4", "suit": "a"},
               {"count": 6, "rank": "F"}])
    b = _hand([{"count": 4, "rank": "2", "suit": "b"},
               {"count": 4, "rank": "4", "suit": "b"},
               {"count": 6, "rank": "F"}])
    assert overlap(a, b) == 6  # only the flowers are certainly shared


def test_stutter_key_ignores_the_tail_but_not_the_numbers():
    core = [{"count": 4, "rank": "2", "suit": "a"},
            {"count": 4, "rank": "4", "suit": "a"},
            {"count": 4, "rank": "6", "suit": "a"}]
    flowers = _hand([*core, {"count": 2, "rank": "F"}])
    winds = _hand([*core, {"count": 2, "rank": "N"}])
    other = _hand([{"count": 4, "rank": "2", "suit": "a"},
                   {"count": 4, "rank": "4", "suit": "a"},
                   {"count": 4, "rank": "8", "suit": "a"},
                   {"count": 2, "rank": "F"}])
    assert stutter_key(flowers) == stutter_key(winds)
    assert stutter_key(flowers) != stutter_key(other)


def test_stutter_key_keeps_pure_honour_lines_apart():
    """With no numeric groups there is nothing else to identify a line by,
    so the whole hand becomes the key — otherwise every winds-and-dragons
    line would read as the same one."""
    winds = _hand([{"count": 4, "rank": "N"}, {"count": 3, "rank": "E"},
                   {"count": 3, "rank": "W"}, {"count": 4, "rank": "S"}])
    dragons = _hand([{"count": 4, "rank": "R"}, {"count": 4, "rank": "G"},
                     {"count": 4, "rank": "soap"}, {"count": 2, "rank": "F"}])
    assert stutter_key(winds) != stutter_key(dragons)


def test_stutter_key_treats_rank_variables_as_identity():
    run = _hand([{"count": 4, "rank": "x", "suit": "a"},
                 {"count": 4, "rank": "x+1", "suit": "a"},
                 {"count": 4, "rank": "x+2", "suit": "a"},
                 {"count": 2, "rank": "F"}])
    assert any("x" in str(part) for part in stutter_key(run))


@pytest.mark.parametrize(
    "groups, concealed, expect_at_least",
    [
        pytest.param(
            [{"count": 4, "rank": "F"}, {"count": 4, "rank": "1", "suit": "a"},
             {"count": 4, "rank": "2", "suit": "a"},
             {"count": 2, "rank": "3", "suit": "a"}],
            False, VALUE_MIN, id="plain-one-suit"),
        pytest.param(
            [{"count": 2, "rank": "1", "suit": "a"},
             {"count": 2, "rank": "3", "suit": "a"},
             {"count": 2, "rank": "5", "suit": "b"},
             {"count": 2, "rank": "7", "suit": "b"},
             {"count": 2, "rank": "9", "suit": "c"},
             {"count": 2, "rank": "R"}, {"count": 2, "rank": "G"}],
            True, 50, id="concealed-all-pairs-three-suits"),
    ],
)
def test_provisional_value_prices_difficulty_upward(
    groups, concealed, expect_at_least
):
    value = provisional_value(_hand(groups, concealed=concealed))
    assert expect_at_least <= value <= VALUE_MAX
    assert value % 5 == 0


def test_pivot_report_counts_every_line(generated):
    _, card = generated
    report = pivot_report(card)
    assert set(report) == {h.id for h in card.hands}
    assert all(v >= 0 for v in report.values())
