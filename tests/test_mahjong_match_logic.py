"""Tests for the §3.3 matcher + reachability — stage 2 of docs/plans/meadow-mahjong.md.

Table-driven per the spec's §10 matcher bullet: every First Light line gets a
positive case with its exact winning 14; then x-binding bounds, suit-binding
conflicts, D-binding, soap-as-zero (a fixture card — First Light has no rank-0
line), joker-in-pair rejection, jokerless counting, concealed-with-exposure
rejection, exposure→group mapping, multi-line settling at max value, and the
amendment-2 reachability rules the Duel fallow payout rides on.
"""

from __future__ import annotations

from collections import Counter

import pytest

from bot_modules.games.mahjong.card_logic import lint_card, load_card, load_first_light
from bot_modules.games.mahjong.match_logic import (
    Exposure,
    best_match,
    fallow_base_value,
    match_hand,
    reachable_lines,
)
from bot_modules.games.mahjong.tiles import Tile


def tiles(spec: str) -> list[Tile]:
    """Build a rack from "code*n code ..." — e.g. "flower*2 2b*3 wn"."""
    out: list[Tile] = []
    for part in spec.split():
        code, _, n = part.partition("*")
        out.extend([Tile(code)] * (int(n) if n else 1))
    return out


@pytest.fixture(scope="module")
def card():
    return load_first_light()


def ids_of(matches):
    return [m.hand.id for m in matches]


# ── Every First Light line has a positive case (spec §10) ────────────────────

POSITIVE_CASES = [
    # (hand_id, tile spec (concealed, no exposures), expected jokers_used)
    ("gh-1", "flower*4 2d*4 6b*4 8c*2", 0),
    ("gh-2", "flower*4 5d*3 5b*3 5c*4", 0),
    ("gh-3", "flower*6 9d*4 9b*4", 0),
    ("mr-1", "flower*2 2b*3 3b*4 4b*3 5b*2", 0),
    ("mr-2", "7d*4 8b*4 9c*4 wn*2", 0),
    ("mr-3", "1c*4 2c*2 3c*4 dg*4", 0),
    ("eg-1", "flower*2 2b*4 4b*3 6b*3 8b*2", 0),
    ("eg-2", "2d*3 4d*3 6b*3 8b*3 2c*2", 0),
    ("eg-3", "flower*4 2d 4d 6d 8d 2b 4b 6b 8b 8c*2", 0),
    ("sb-1", "flower*2 1d*3 3d*3 5d*3 7d*3", 0),
    ("sb-2", "1d*4 3d*2 5b*4 7b*2 9c*2", 0),
    ("sb-3", "1d*3 3b*4 5d*3 7b*4", 0),
    ("ws-1", "wn*4 we*3 ww*3 ws*4", 0),
    ("ws-2", "flower*2 wn*4 ws*4 dr*4", 0),
    ("ws-3", "flower*2 dr*4 dg*4 soap*4", 0),
    ("ws-4", "wn*2 we*2 ww*2 ws*2 dr*3 dg*3", 0),
    # Tall Timber quints demand jokers by construction (4 naturals exist)
    ("tt-1", "3d*4 joker 4d*4 5d*4 joker", 2),
    ("tt-2", "wn*4 joker 6d*4 joker 6b*4", 2),
    # ... except Old Growth's flower quint, drawable natural from 8
    ("tt-3", "flower*5 2d*4 joker 2b*4", 1),
    ("qp-1", "flower*2 1d*2 3d*2 5d*2 7d*2 9d*2 wn*2", 0),
    ("qp-2", "flower*2 4c*2 5c*2 6c*2 7c*2 8c*2 wn*2", 0),
    ("qp-3", "1d*2 2d*2 3d*2 1b*2 2b*2 3b*2 dr*2", 0),
]


@pytest.mark.parametrize(
    "hand_id, spec, jokers", POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES]
)
def test_every_first_light_line_matches_its_winning_14(card, hand_id, spec, jokers):
    rack = tiles(spec)
    assert len(rack) == 14
    matches = match_hand(rack, [], card)
    assert hand_id in ids_of(matches)
    match = next(m for m in matches if m.hand.id == hand_id)
    assert match.jokers_used == jokers
    assert match.jokerless == (jokers == 0)


# ── Binding rules ────────────────────────────────────────────────────────────


def test_x_binding_bounds(card):
    # runs live at both ends of 1–9 …
    assert "mr-1" in ids_of(match_hand(tiles("flower*2 1b*3 2b*4 3b*3 4b*2"), [], card))
    assert "mr-1" in ids_of(match_hand(tiles("flower*2 6b*3 7b*4 8b*3 9b*2"), [], card))
    # … but never wrap 9 → 1
    assert match_hand(tiles("flower*2 8b*3 9b*4 1b*3 2b*2"), [], card) == []


def test_same_letter_means_same_suit(card):
    # mr-1's run is one suit; split the x+1 kong into dots and it dies
    assert match_hand(tiles("flower*2 2b*3 3d*4 4b*3 5b*2"), [], card) == []


def test_distinct_letters_mean_distinct_suits(card):
    # sb-3 is 3(1)a 4(3)b 3(5)a 4(7)b — all one suit must NOT match
    assert match_hand(tiles("1d*3 3d*4 5d*3 7d*4"), [], card) == []
    # and the honest two-suit version does
    assert "sb-3" in ids_of(match_hand(tiles("1d*3 3b*4 5d*3 7b*4"), [], card))


def test_x_binds_once_per_hand(card):
    # gh-2 is 3(x)a 3(x)b 4(x)c — one rank across three suits; mixed ranks die
    assert "gh-2" in ids_of(match_hand(tiles("flower*4 5d*3 5b*3 5c*4"), [], card))
    assert match_hand(tiles("flower*4 5d*3 6b*3 7c*4"), [], card) == []


def test_dragon_binding(card):
    # mr-3's 4(D) takes any single dragon…
    for dragon, code in (("dr", "dr"), ("dg", "dg"), ("soap", "soap")):
        matches = match_hand(tiles(f"1c*4 2c*2 3c*4 {code}*4"), [], card)
        match = next(m for m in matches if m.hand.id == "mr-3")
        assert match.dragon == dragon
    # …but never a mix
    assert match_hand(tiles("1c*4 2c*2 3c*4 dr*2 dg*2"), [], card) == []


# ── Soap as zero (fixture card — spec §10 asks for a rank-0 line) ────────────

SOAP_CARD = load_card({
    "card_id": "test-soap", "display_name": "Soap Fixture", "season": "t",
    "hands": [
        {"id": "zero-1", "section": "Zeros", "name": "Year Zero",
         "concealed": False, "value": 30,
         "groups": [{"count": 3, "rank": "2", "suit": "a"}, {"count": 4, "rank": "0"},
                    {"count": 3, "rank": "2", "suit": "b"}, {"count": 2, "rank": "6", "suit": "c"},
                    {"count": 2, "rank": "0"}]},
        {"id": "zero-2", "section": "Zeros", "name": "Round Numbers",
         "concealed": False, "value": 25,
         "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "0"},
                    {"count": 4, "rank": "1", "suit": "a"}, {"count": 2, "rank": "3", "suit": "a"}]},
    ],
})


def test_soap_fixture_lints_clean():
    assert lint_card(SOAP_CARD).errors == []


def test_soap_satisfies_rank_zero():
    matches = match_hand(tiles("2d*3 soap*4 2b*3 6c*2 soap*2"), [], SOAP_CARD)
    assert "zero-1" in ids_of(matches)


def test_soap_zero_pair_needs_naturals_but_kong_takes_jokers():
    # 4 naturals: pair takes 2, the kong runs 2 naturals + 2 jokers
    matches = match_hand(tiles("2d*3 soap*2 joker*2 2b*3 6c*2 soap*2"), [], SOAP_CARD)
    match = next(m for m in matches if m.hand.id == "zero-1")
    assert match.jokers_used == 2
    # with 5 soaps held the matcher reallocates: pair keeps naturals, the
    # kong hosts the joker — still legal, one joker used
    five = match_hand(tiles("2d*3 soap*4 2b*3 6c*2 soap joker"), [], SOAP_CARD)
    assert next(m for m in five if m.hand.id == "zero-1").jokers_used == 1
    # but ONE soap can't cover the pair even with jokers to burn — a joker
    # never stands in a count <= 2 group
    assert "zero-1" not in ids_of(
        match_hand(tiles("2d*3 soap 2b*3 6c*2 joker*5"), [], SOAP_CARD)
    )


def test_dragons_arent_zeros_and_zeros_arent_dragons():
    # red/green never satisfy rank 0
    assert match_hand(tiles("2d*3 dr*4 2b*3 6c*2 dr*2"), [], SOAP_CARD) == []


# ── Joker legality (§2.6) ────────────────────────────────────────────────────


def test_joker_never_stands_in_a_pair(card):
    assert match_hand(tiles("flower*4 2d*4 6b*4 8c joker"), [], card) == []


def test_joker_never_stands_in_a_single(card):
    # eg-3's singles: replace one single with a joker → dead
    assert match_hand(
        tiles("flower*4 2d 4d 6d 8d 2b 4b 6b joker 8c*2"), [], card
    ) == []


def test_joker_fills_a_short_3plus_group_but_naturals_never_dangle(card):
    # a joker completes the short kong — ws-1 matches, one joker used
    matches = match_hand(tiles("wn*4 we*3 ww*3 ws*3 joker"), [], card)
    assert next(m for m in matches if m.hand.id == "ws-1").jokers_used == 1
    # a natural with no slot on the line is a leftover — no match
    assert match_hand(tiles("wn*4 we*3 ww*3 ws*3 dr"), [], card) == []


def test_jokerless_flag_counts_exposure_jokers(card):
    concealed = tiles("flower*4 2d*4 8c*2")
    clean = match_hand(concealed, [Exposure(Tile("6b"), 4)], card)
    with_joker = match_hand(concealed, [Exposure(Tile("6b"), 4, jokers=1)], card)
    assert next(m for m in clean if m.hand.id == "gh-1").jokerless
    match = next(m for m in with_joker if m.hand.id == "gh-1")
    assert match.jokers_used == 1 and not match.jokerless


# ── Concealed lines and exposures (§2.5, §3.3) ───────────────────────────────


def test_concealed_line_rejects_any_exposure(card):
    concealed = tiles("flower*2 1d*2 3d*2 5d*2 7d*2 9d")
    assert "qp-1" not in ids_of(match_hand(concealed, [Exposure(Tile.NORTH, 3)], card))


def test_exposure_must_map_exact_count(card):
    # a 6b PUNG can't fill gh-1's 4(6)b kong, even with the 4th 6b in hand
    concealed = tiles("flower*4 2d*4 6b 8c*2")
    assert match_hand(concealed, [Exposure(Tile("6b"), 3)], card) == []


def test_exposure_wrong_natural_never_maps(card):
    concealed = tiles("flower*4 2d*4 8c*2")
    assert match_hand(concealed, [Exposure(Tile("5b"), 4)], card) == []


def test_two_exposures_map_injectively(card):
    matches = match_hand(
        tiles("flower*6"),
        [Exposure(Tile("9d"), 4), Exposure(Tile("9b"), 4)],
        card,
    )
    assert "gh-3" in ids_of(matches)


def test_flower_exposure_with_joker(card):
    matches = match_hand(
        tiles("2d*4 6b*4 8c*2"), [Exposure(Tile.FLOWER, 4, jokers=1)], card
    )
    match = next(m for m in matches if m.hand.id == "gh-1")
    assert match.jokers_used == 1


def test_exposure_invariants():
    with pytest.raises(ValueError):
        Exposure(Tile.JOKER, 3)
    with pytest.raises(ValueError):
        Exposure(Tile.NORTH, 2)  # pairs can't be called
    with pytest.raises(ValueError):
        Exposure(Tile.NORTH, 3, jokers=4)


# ── Sizes and multi-line settle ──────────────────────────────────────────────


def test_wrong_tile_totals_never_match(card):
    assert match_hand(tiles("wn*4 we*3 ww*3 ws*3"), [], card) == []  # 13
    assert match_hand(tiles("wn*4 we*3 ww*3 ws*4 dr"), [], card) == []  # 15
    assert match_hand(tiles("wn*4 we*3 ww*3 ws*4"), [Exposure(Tile.RED, 3)], card) == []


MULTI_CARD = load_card({
    "card_id": "test-multi", "display_name": "Multi Fixture", "season": "t",
    "hands": [
        {"id": "lo", "section": "S", "name": "Any Run", "concealed": False, "value": 25,
         "groups": [{"count": 2, "rank": "F"}, {"count": 4, "rank": "x", "suit": "a"},
                    {"count": 4, "rank": "x+1", "suit": "a"}, {"count": 4, "rank": "x+2", "suit": "a"}]},
        {"id": "hi", "section": "S", "name": "One Two Three", "concealed": False, "value": 40,
         "groups": [{"count": 2, "rank": "F"}, {"count": 4, "rank": "1", "suit": "a"},
                    {"count": 4, "rank": "2", "suit": "a"}, {"count": 4, "rank": "3", "suit": "a"}]},
    ],
})


def test_multi_line_match_settles_at_max_value():
    matches = match_hand(tiles("flower*2 1d*4 2d*4 3d*4"), [], MULTI_CARD)
    assert sorted(ids_of(matches)) == ["hi", "lo"]
    best = best_match(matches)
    assert best is not None and best.hand.id == "hi" and best.hand.value == 40
    assert best_match([]) is None


# ── Reachability (amendment 2 — Duel fallow payout) ──────────────────────────


def test_fresh_hand_reaches_every_line(card):
    rack = tiles("1d*2 2d*2 3d*2 wn*2 flower*2 9c*3")
    live = reachable_lines(rack, [], card, Counter())
    assert len(live) == 22
    assert fallow_base_value(rack, [], card, Counter()) == 25


def test_discards_kill_a_pair_line_but_not_a_jokerable_group(card):
    rack = tiles("1d*2 2d*2 3d*2 wn*2 flower*2 9c*3")
    # all four red dragons visible elsewhere: qp-3 (2(R) pair) is dead …
    seen = Counter({Tile.RED: 4})
    live_ids = [h.id for h in reachable_lines(rack, [], card, seen)]
    assert "qp-3" not in live_ids
    # … but ws-3 (4(R) kong) survives — jokers can cover a whole 3+ group
    assert "ws-3" in live_ids
    # kill the jokers too and the kong dies with them
    seen = Counter({Tile.RED: 4, Tile.JOKER: 8})
    assert "ws-3" not in [h.id for h in reachable_lines(rack, [], card, seen)]


def test_exposure_kills_concealed_lines_in_reachability(card):
    rack = tiles("1d*2 2d*2 3d*2 wn*2 flower*2")
    live_ids = [h.id for h in reachable_lines(rack, [Exposure(Tile("9c"), 3)], card, Counter())]
    for concealed_id in ("eg-3", "ws-4", "qp-1", "qp-2", "qp-3"):
        assert concealed_id not in live_ids


def test_exposure_must_be_absorbable_for_liveness(card):
    rack = tiles("1d*2 2d*2 3d*2 flower*2 wn*2")
    live_ids = [h.id for h in reachable_lines(rack, [Exposure(Tile("6b"), 4)], card, Counter())]
    assert "ws-1" not in live_ids   # all-winds line can't hold a suited kong
    assert "gh-1" in live_ids       # 4(6)b is literally one of its groups


def test_own_tiles_count_against_the_unseen_pool(card):
    # quints: 5-of-a-kind exceeds the 4 naturals, so a suited quint lives
    # only through jokers — kill all 8 and every Tall Timber line dies
    # (Old Growth's flower quint is natural-drawable, but its 5(x)a is not)
    rack = tiles("3d*4 4d*4 5d*4 flower")
    fresh = [h.id for h in reachable_lines(rack, [], card, Counter())]
    assert {"tt-1", "tt-2", "tt-3"} <= set(fresh)
    seen = Counter({Tile.JOKER: 8})
    live_ids = [h.id for h in reachable_lines(rack, [], card, seen)]
    assert not {"tt-1", "tt-2", "tt-3"} & set(live_ids)
    # non-quint shapes shrug: gh-3 (6F + two kongs) needs no jokers
    assert "gh-3" in live_ids


def test_fallow_base_falls_back_to_card_minimum(card):
    # two suited kongs no line can hold together → nothing live → card min
    rack = tiles("wn*2 we*2 ww*2")
    exposures = [Exposure(Tile("1d"), 4), Exposure(Tile("9c"), 4)]
    assert reachable_lines(rack, exposures, card, Counter()) == []
    assert fallow_base_value(rack, exposures, card, Counter()) == 25


def test_fallow_base_tracks_the_lowest_live_line(card):
    # exposures that fit only Tall Timber shapes: 5(N) + 5(6d) → tt-2 (40)
    rack = tiles("6b*3")
    exposures = [Exposure(Tile.NORTH, 5, jokers=1), Exposure(Tile("6d"), 5, jokers=1)]
    live = reachable_lines(rack, exposures, card, Counter())
    assert [h.id for h in live] == ["tt-2"]
    assert fallow_base_value(rack, exposures, card, Counter()) == 40


# ── Assistance: closest_lines / dangerous_tiles / suggest_discard ────────────
# (plans/mahjong-assist.md stage 1; decisions A2–A6)

from bot_modules.games.mahjong.match_logic import (  # noqa: E402
    Prospect,
    closest_lines,
    dangerous_tiles,
    suggest_discard,
)


def prospect_for(prospects: list[Prospect], hand_id: str) -> Prospect:
    return next(p for p in prospects if p.hand.id == hand_id)


def needed_spec(p: Prospect) -> str:
    return " ".join(f"{t.code}*{n}" if n > 1 else t.code for t, n in p.needed)


@pytest.mark.parametrize(
    "hand_id, spec, jokers", POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES]
)
def test_a_winning_14_sits_at_distance_zero(card, hand_id, spec, jokers):
    # Cross-check against the exact matcher: distance 0 ⟺ match_hand agrees.
    prospects = closest_lines(tiles(spec), [], card, Counter(), limit=None)
    p = prospect_for(prospects, hand_id)
    assert p.distance == 0
    assert p.needed == ()
    assert p.dead_weight == ()
    assert prospects[0].distance == 0  # rank 1 is a completed line


def test_one_tile_short_is_distance_one(card):
    # gh-1 minus one 8c: 13 tiles, exactly the pair tile missing.
    prospects = closest_lines(
        tiles("flower*4 2d*4 6b*4 8c"), [], card, Counter(), limit=None
    )
    p = prospect_for(prospects, "gh-1")
    assert p.distance == 1
    assert p.needed == ((Tile.CRAK8, 1),)
    assert p.dead_weight == ()


def test_needed_always_sums_to_distance(card):
    # The A2 invariant, across every line, on an awkward mixed rack.
    rack = tiles("flower*2 1d*2 9d 2b*3 wn*2 dr joker*2")
    for p in closest_lines(rack, [], card, Counter(), limit=None):
        assert sum(n for _, n in p.needed) == p.distance
        assert p.distance >= 0


def test_held_jokers_shrink_a_3plus_gap_but_never_a_pair(card):
    # gh-1 = F*4 2(a)*4 6(b)*4 8(c)*2. Hold the two kongs complete, the
    # flower kong 2 short, the pair missing, plus two jokers: the jokers
    # cover the flower gap (3+), never the 8c pair — distance is the pair.
    rack = tiles("flower*2 2d*4 6b*4 joker*2")
    p = prospect_for(closest_lines(rack, [], card, Counter(), limit=None), "gh-1")
    assert p.distance == 2
    assert p.needed == ((Tile.CRAK8, 2),)


def test_a_spare_joker_is_never_dead_weight(card):
    # Complete gh-1 in 12 tiles' worth of naturals + 2 jokers where only one
    # gap is joker-eligible: the spare joker must not appear as dead weight.
    rack = tiles("flower*3 2d*4 6b*4 8c*2 joker")  # 14 held, kong one short
    p = prospect_for(closest_lines(rack, [], card, Counter(), limit=None), "gh-1")
    assert p.distance == 0  # joker completes the flower kong
    rack = tiles("flower*4 2d*4 6b*4 8c joker")  # pair short; joker can't help
    p = prospect_for(closest_lines(rack, [], card, Counter(), limit=None), "gh-1")
    assert p.distance == 1
    assert Tile.JOKER not in dict(p.dead_weight)
    assert Tile.JOKER not in dict(p.needed)


def test_dead_weight_lists_what_the_line_cannot_use(card):
    # 9d*3 fits no gh-1 group; the excess beyond demand is dead weight.
    rack = tiles("flower*4 2d*4 6b*2 9d*3")
    p = prospect_for(closest_lines(rack, [], card, Counter(), limit=None), "gh-1")
    assert dict(p.dead_weight) == {Tile.DOT9: 3}


def test_an_extinct_pair_kills_the_line_here_too(card):
    # gh-1's pair is an 8 of *some* suit — the suit letters permute, so one
    # suit dying merely re-binds the line. Only when every suit's 8s are
    # exhausted (3 seen + the 1 held accounts for all four 8c) is the pair
    # truly undrawable and the line excluded outright (A3).
    rack = tiles("flower*4 2d*4 6b*4 8c")
    partial = Counter({Tile.CRAK8: 3})
    assert any(
        p.hand.id == "gh-1"
        for p in closest_lines(rack, [], card, partial, limit=None)
    )
    extinct = Counter({Tile.CRAK8: 3, Tile.DOT8: 4, Tile.BAM8: 4})
    assert all(
        p.hand.id != "gh-1"
        for p in closest_lines(rack, [], card, extinct, limit=None)
    )


def test_a_drawn_joker_keeps_a_binding_alive_at_zero_copies(card):
    # sb-1 under a→dots is 2 away with every unheld 7d gone — but a pung
    # gap can still be filled by *drawn* jokers, so the binding stays live
    # and the distance stays 2. The needed list honestly names the 7d.
    rack = tiles("flower*2 1d*3 3d*3 5d*3 7d")
    seen = Counter({Tile.DOT7: 3})
    p = prospect_for(closest_lines(rack, [], card, seen, limit=None), "sb-1")
    assert p.distance == 2
    assert dict(p.needed) == {Tile.DOT7: 2}


def test_a_dead_binding_never_sets_the_distance(card):
    # Same rack, but the jokers are exhausted too: now the a→dots binding
    # (2 away) is genuinely dead, and the report must carry a live sister
    # binding at its greater distance — never the dead binding's lie.
    rack = tiles("flower*2 1d*3 3d*3 5d*3 7d")
    seen = Counter({Tile.DOT7: 3, Tile.JOKER: 8})
    p = prospect_for(closest_lines(rack, [], card, seen, limit=None), "sb-1")
    assert p.distance > 2
    for tile, _ in p.needed:
        assert tile is not Tile.DOT7


def test_exposures_lock_their_groups_and_count_as_matched(card):
    # gh-1 with the 2d kong already exposed: 10 concealed tiles, pair short.
    exp = [Exposure(natural=Tile.DOT2, count=4, jokers=1)]
    rack = tiles("flower*4 6b*4 8c")
    p = prospect_for(closest_lines(rack, exp, card, Counter(), limit=None), "gh-1")
    assert p.distance == 1
    assert p.needed == ((Tile.CRAK8, 1),)


def test_an_unabsorbable_exposure_kills_the_line(card):
    # A wind pung fits no gh-1 group, so gh-1 must not appear at all.
    exp = [Exposure(natural=Tile.NORTH, count=3)]
    prospects = closest_lines(tiles("flower*4 2d*4 8c*2"), exp, card, Counter(), limit=None)
    assert all(p.hand.id != "gh-1" for p in prospects)


def test_concealed_lines_vanish_once_exposed(card):
    exp = [Exposure(natural=Tile.DOT1, count=4)]
    prospects = closest_lines(tiles("3d*2 5b*4 7b*2 9c*2"), exp, card, Counter(), limit=None)
    concealed_ids = {h.id for h in card.hands if h.concealed}
    assert concealed_ids
    assert concealed_ids.isdisjoint({p.hand.id for p in prospects})


def test_all_lines_dead_returns_empty(card):
    # Every flower and every joker gone from the world, rack of singles:
    # nothing on First Light survives... craft via a tiny fixture instead.
    solo = load_card({
        "card_id": "t-solo", "display_name": "Solo", "season": "t",
        "hands": [{"id": "s-1", "section": "S", "name": "Only", "concealed": False,
                   "value": 25,
                   "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "N"},
                              {"count": 4, "rank": "1", "suit": "a"},
                              {"count": 2, "rank": "R"}]}],
    })
    seen = Counter({Tile.FLOWER: 8, Tile.JOKER: 8})
    assert closest_lines(tiles("wn 1d dr"), [], solo, seen, limit=None) == []


TIE_CARD = load_card({
    "card_id": "t-tie", "display_name": "Ties", "season": "t",
    "hands": [
        {"id": "low-first", "section": "T", "name": "Low First", "concealed": False,
         "value": 25,
         "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "N"},
                    {"count": 4, "rank": "S"}, {"count": 2, "rank": "R"}]},
        {"id": "high", "section": "T", "name": "High", "concealed": False,
         "value": 30,
         "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "N"},
                    {"count": 4, "rank": "E"}, {"count": 2, "rank": "G"}]},
        {"id": "low-second", "section": "T", "name": "Low Second", "concealed": False,
         "value": 25,
         "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "N"},
                    {"count": 4, "rank": "W"}, {"count": 2, "rank": "0"}]},
    ],
})


def test_tie_break_is_value_then_card_order():
    # Rack feeds all three lines identically (F*4 N*4 held): all tie on
    # distance 6 → high value first, then the two 25s in card order.
    prospects = closest_lines(tiles("flower*4 wn*4 2b*2"), [], TIE_CARD, Counter(), limit=None)
    assert [p.hand.id for p in prospects] == ["high", "low-first", "low-second"]
    assert len({p.distance for p in prospects}) == 1


def test_limit_trims_after_ranking():
    prospects = closest_lines(tiles("flower*4 wn*4 2b*2"), [], TIE_CARD, Counter(), limit=2)
    assert [p.hand.id for p in prospects] == ["high", "low-first"]


def test_ranking_is_stable_across_calls(card):
    rack = tiles("flower*2 1d*2 9d 2b*3 wn*2 dr joker*2")
    first = closest_lines(rack, [], card, Counter(), limit=None)
    second = closest_lines(rack, [], card, Counter(), limit=None)
    assert [(p.hand.id, p.distance, p.needed) for p in first] == [
        (p.hand.id, p.distance, p.needed) for p in second
    ]


# ── dangerous_tiles (A6) ─────────────────────────────────────────────────────


def test_no_exposures_reveal_nothing(card):
    assert dangerous_tiles(card, [[], []]) == frozenset()


def test_exposures_mark_compatible_lines_remaining_demand(card):
    # A 9d kong is compatible with gh-3 (F*6 9(a)*4 9(b)*4) among others;
    # the danger set must carry the naturals those lines still want — the
    # flower and the sister nines — and no suit the exposure rules out.
    danger = dangerous_tiles(card, [[Exposure(natural=Tile.DOT9, count=4)]])
    assert Tile.FLOWER in danger
    assert Tile.BAM9 in danger and Tile.CRAK9 in danger


def test_dead_weight_never_suggests_feeding_a_visible_threat(card):
    # The A6 rail end-to-end: the only dead-weight tile is also the visible
    # threat's want, so coach must stay silent rather than suggest it.
    rack = tiles("flower*4 2d*4 6b*2 9b*3")
    prospects = closest_lines(rack, [], card, Counter(), limit=None)
    assert dict(prospects[0].dead_weight) == {Tile.BAM9: 3}
    danger = dangerous_tiles(card, [[Exposure(natural=Tile.DOT9, count=4)]])
    assert Tile.BAM9 in danger
    assert suggest_discard(rack, prospects, danger) is None


# ── suggest_discard (A5/A6) ──────────────────────────────────────────────────


def test_suggests_the_tile_fewest_lines_can_use(card):
    # 9d feeds gh-3 and others; wn feeds mr-2/ws lines; the pick must be the
    # one consumed by fewer live lines — computed, not asserted blind.
    rack = tiles("flower*4 2d*4 6b*2 9d wn")
    prospects = closest_lines(rack, [], card, Counter(), limit=None)
    dead = dict(prospects[0].dead_weight)
    assert set(dead) == {Tile.DOT9, Tile.NORTH}
    have = Counter(t for t in rack if t is not Tile.JOKER)
    use = {
        t: sum(1 for p in prospects if have[t] - dict(p.dead_weight).get(t, 0) > 0)
        for t in dead
    }
    order = {tile: i for i, tile in enumerate(Tile)}
    expected = min(use, key=lambda t: (use[t], order[t]))
    assert suggest_discard(rack, prospects) is expected


def test_never_suggests_a_joker(card):
    # Force a rack whose only excess is jokers: no suggestion at all.
    rack = tiles("flower*4 2d*4 6b*4 8c joker")
    prospects = closest_lines(rack, [], card, Counter(), limit=None)
    assert prospects[0].distance <= 1
    assert suggest_discard(rack, prospects) is None


def test_no_prospects_no_suggestion():
    assert suggest_discard(tiles("1d 2d"), []) is None


def test_all_candidates_dangerous_means_silence(card):
    rack = tiles("flower*4 2d*4 6b*2 9d*3")
    prospects = closest_lines(rack, [], card, Counter(), limit=None)
    dead = [t for t, _ in prospects[0].dead_weight]
    assert suggest_discard(rack, prospects, frozenset(dead)) is None


# ── Invariant sweep: random racks, engine-level properties ───────────────────


@pytest.mark.parametrize("seed", range(20))
def test_assist_invariants_hold_on_random_racks(card, seed):
    import random

    from bot_modules.games.mahjong.tiles import build_deck

    rng = random.Random(seed)
    wall = build_deck()
    rng.shuffle(wall)
    hold = rng.choice((13, 14))
    rack, seen = wall[:hold], Counter(wall[hold : hold + rng.randrange(0, 60)])

    prospects = closest_lines(rack, [], card, seen, limit=None)

    # Same liveness, same exclusions: closest_lines sees exactly the lines
    # reachable_lines does — one enumerator, one truth.
    assert {p.hand.id for p in prospects} == {
        h.id for h in reachable_lines(rack, [], card, seen)
    }

    order = {tile: i for i, tile in enumerate(Tile)}
    previous = None
    for rank, p in enumerate(prospects):
        assert sum(n for _, n in p.needed) == p.distance
        assert all(n > 0 for _, n in p.needed)
        assert all(n > 0 for _, n in p.dead_weight)
        assert Tile.JOKER not in dict(p.needed)
        assert Tile.JOKER not in dict(p.dead_weight)
        assert [t for t, _ in p.needed] == sorted(
            (t for t, _ in p.needed), key=order.__getitem__
        )
        if previous is not None:
            assert previous.distance <= p.distance
            if previous.distance == p.distance:
                assert previous.hand.value >= p.hand.value
        previous = p
        # Distance zero and only distance zero is an exact match (needs 14).
        if hold == 14:
            matched_ids = {m.hand.id for m in match_hand(rack, [], card)}
            assert (p.distance == 0) == (p.hand.id in matched_ids)

    pick = suggest_discard(rack, prospects)
    if pick is not None:
        assert pick is not Tile.JOKER
        for p in prospects[:3]:  # dead for EVERY shown hand, needed by none
            assert pick in dict(p.dead_weight)
            assert pick not in dict(p.needed)


# ── Review round 2 (code-review 2026-08-22): coach self-consistency ──────────


def test_suggestion_never_contradicts_a_shown_hands_need(card):
    # The reproduced contradiction: this rack used to get "discard 5d" while
    # shown hand #3 (sb-2) printed "need 5d ×3" in the same embed. The
    # suggestion must come from the dead-weight intersection of the hands
    # actually shown — the same set the embed prints — which provably
    # excludes every shown hand's needed tile.
    rack = tiles("1c 1c 2b 2b 2b 3c 5c 5d 8b 9b dr soap ww")
    prospects = closest_lines(rack, [], card, Counter(), limit=None)
    shown = prospects[:3]
    assert any(Tile.DOT5 in dict(p.needed) for p in shown)  # the trap exists
    pick = suggest_discard(rack, prospects)
    assert pick is not Tile.DOT5
    if pick is not None:
        for p in shown:
            assert pick not in dict(p.needed)
        # and it is dead weight for every shown hand, not just the closest
        for p in shown:
            assert pick in dict(p.dead_weight)
