"""Tests for mahjong tiles + card model + linter — stage 1 of docs/plans/meadow-mahjong.md.

The First Light transcription test embeds the spec §4 table *verbatim* and
checks the loaded card regenerates it — the JSON and this table were
transcribed independently, so a typo in either fails here. Linter cases are
one row per §3.4 rule, failures and boundary passes both.
"""

from __future__ import annotations

import importlib.util
import json
import random
from collections import Counter
from pathlib import Path

import pytest

from bot_modules.games.mahjong.card_logic import (
    FIRST_LIGHT_PATH,
    CardError,
    RankKind,
    lint_card,
    lint_card_data,
    load_card,
    load_card_file,
    load_first_light,
)
from bot_modules.games.mahjong.tiles import (
    DECK_SIZE,
    Tile,
    build_deck,
    copies,
    shuffled_wall,
    sort_rack,
)

# ── First Light transcription (spec §4, rows verbatim) ───────────────────────

SPEC_TABLE = [
    # (id, section, name, groups, value, X/C)
    ("gh-1", "Golden Hour", "Golden Hour", "4(F) 4(2)a 4(6)b 2(8)c", 25, "X"),
    ("gh-2", "Golden Hour", "Dawn Chorus", "4(F) 3(x)a 3(x)b 4(x)c", 25, "X"),
    ("gh-3", "Golden Hour", "First Light", "6(F) 4(x)a 4(x)b", 30, "X"),
    ("mr-1", "Meadow Runs", "Meadow Run", "2(F) 3(x)a 4(x+1)a 3(x+2)a 2(x+3)a", 30, "X"),
    ("mr-2", "Meadow Runs", "Terrace Run", "4(x)a 4(x+1)b 4(x+2)c 2(N)", 35, "X"),
    ("mr-3", "Meadow Runs", "River Bend", "4(x)a 2(x+1)a 4(x+2)a 4(D)", 30, "X"),
    ("eg-1", "Even Ground", "Even Ground", "2(F) 4(2)a 3(4)a 3(6)a 2(8)a", 25, "X"),
    ("eg-2", "Even Ground", "Split Rail", "3(2)a 3(4)a 3(6)b 3(8)b 2(2)c", 30, "X"),
    ("eg-3", "Even Ground", "Skipping Stones",
     "4(F) 1(2)a 1(4)a 1(6)a 1(8)a 1(2)b 1(4)b 1(6)b 1(8)b 2(8)c", 40, "C"),
    ("sb-1", "Switchbacks", "Trailheads", "2(F) 3(1)a 3(3)a 3(5)a 3(7)a", 25, "X"),
    ("sb-2", "Switchbacks", "Scree", "4(1)a 2(3)a 4(5)b 2(7)b 2(9)c", 35, "X"),
    ("sb-3", "Switchbacks", "Ridgeline", "3(1)a 4(3)b 3(5)a 4(7)b", 30, "X"),
    ("ws-1", "Windstorm", "Four Winds", "4(N) 3(E) 3(W) 4(S)", 30, "X"),
    ("ws-2", "Windstorm", "High Lonesome", "2(F) 4(N) 4(S) 4(D)", 25, "X"),
    ("ws-3", "Windstorm", "Fire on the Mountain", "2(F) 4(R) 4(G) 4(soap)", 35, "X"),
    ("ws-4", "Windstorm", "Weathervane", "2(N) 2(E) 2(W) 2(S) 3(R) 3(G)", 40, "C"),
    ("tt-1", "Tall Timber", "Sequoia", "5(x)a 4(x+1)a 5(x+2)a", 45, "X"),
    ("tt-2", "Tall Timber", "Lodgepole", "5(N) 5(x)a 4(x)b", 40, "X"),
    ("tt-3", "Tall Timber", "Old Growth", "5(F) 5(x)a 4(x)b", 45, "X"),
    ("qp-1", "Quiet Pairs", "Quiet Pairs", "2(F) 2(1)a 2(3)a 2(5)a 2(7)a 2(9)a 2(N)", 50, "C"),
    ("qp-2", "Quiet Pairs", "The Long Trail",
     "2(F) 2(x)a 2(x+1)a 2(x+2)a 2(x+3)a 2(x+4)a 2(N)", 50, "C"),
    ("qp-3", "Quiet Pairs", "Echo", "2(1)a 2(2)a 2(3)a 2(1)b 2(2)b 2(3)b 2(R)", 75, "C"),
]


@pytest.fixture(scope="module")
def first_light():
    return load_first_light()


def test_first_light_has_exactly_the_spec_hands(first_light):
    assert [h.id for h in first_light.hands] == [row[0] for row in SPEC_TABLE]
    assert first_light.card_id == "meadow-first-light"
    assert first_light.display_name == "Meadow Card — First Light"
    assert first_light.season == "2026-autumn"
    assert first_light.max_value == 75


@pytest.mark.parametrize(
    "hand_id, section, name, groups, value, xc",
    SPEC_TABLE,
    ids=[row[0] for row in SPEC_TABLE],
)
def test_first_light_transcription(first_light, hand_id, section, name, groups, value, xc):
    hand = first_light.hand_by_id(hand_id)
    assert hand is not None
    assert hand.section == section
    assert hand.name == name
    assert hand.display == groups  # generated display == spec §4 Groups column
    assert hand.value == value
    assert hand.concealed == (xc == "C")
    assert hand.tile_total == 14


def test_first_light_lints_clean(first_light):
    report = lint_card(first_light)
    assert report.errors == []
    assert report.warnings == []
    assert report.ok


def test_first_light_sections_in_card_order(first_light):
    assert first_light.sections() == [
        "Golden Hour", "Meadow Runs", "Even Ground", "Switchbacks",
        "Windstorm", "Tall Timber", "Quiet Pairs",
    ]


def test_cli_script_agrees_with_the_library():
    """scripts/validate_card.py shares lint_card_data — prove the wiring."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "validate_card.py"
    if not script.exists():  # partial checkout safety; scripts/ is synced today
        pytest.skip("scripts/ not present")
    spec = importlib.util.spec_from_file_location("mm_validate_card", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.lint_file(FIRST_LIGHT_PATH) is True
    assert mod.main([]) == 2
    assert mod.main([str(FIRST_LIGHT_PATH)]) == 0


def test_cli_script_fails_on_a_bad_card(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "validate_card.py"
    if not script.exists():
        pytest.skip("scripts/ not present")
    spec = importlib.util.spec_from_file_location("mm_validate_card2", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bad = tmp_path / "bad.json"
    bad.write_text('{"card_id": "x"}', encoding="utf-8")
    assert mod.lint_file(bad) is False
    notjson = tmp_path / "notjson.json"
    notjson.write_text("{", encoding="utf-8")
    assert mod.main([str(bad), str(notjson), str(tmp_path / "missing.json")]) == 1


# ── Tiles & deck (spec §2.1) ─────────────────────────────────────────────────


def test_deck_is_152_with_spec_copies():
    deck = build_deck()
    assert len(deck) == DECK_SIZE == 152
    counts = Counter(deck)
    for tile in Tile:
        expected = 8 if tile in (Tile.FLOWER, Tile.JOKER) else 4
        assert counts[tile] == expected == copies(tile)


def test_tile_kind_partitions():
    suited = [t for t in Tile if t.is_suited]
    winds = [t for t in Tile if t.is_wind]
    dragons = [t for t in Tile if t.is_dragon]
    assert len(suited) == 27
    assert len(winds) == 4
    assert len(dragons) == 3
    assert sum(1 for t in Tile if t.is_flower) == 1
    assert sum(1 for t in Tile if t.is_joker) == 1
    assert len(list(Tile)) == 36
    for t in Tile:
        assert t.is_honor == (t.is_wind or t.is_dragon)
        # exactly one classification per tile
        assert [t.is_suited, t.is_wind, t.is_dragon, t.is_flower, t.is_joker].count(True) == 1


def test_tile_codes_roundtrip():
    for tile in Tile:
        assert Tile(tile.code) is tile
    assert Tile("5b") is Tile.BAM5
    assert Tile("soap") is Tile.SOAP
    assert Tile.SOAP.rank is None  # zero-matching is a card rule, not a tile fact
    assert Tile.DOT7.suit == "d" and Tile.DOT7.rank == 7
    assert Tile.NORTH.suit is None


def test_shuffle_is_a_permutation_and_injectable():
    wall = shuffled_wall()
    assert Counter(wall) == Counter(build_deck())
    # deterministic under an injected rng — what game tests will lean on
    a = shuffled_wall(random.Random(42))
    b = shuffled_wall(random.Random(42))
    assert a == b


def test_sort_rack_orders_suits_winds_dragons_flowers_jokers():
    rack = [Tile.JOKER, Tile.FLOWER, Tile.SOAP, Tile.SOUTH, Tile.CRAK1,
            Tile.BAM9, Tile.DOT2, Tile.RED, Tile.NORTH]
    assert [t.code for t in sort_rack(rack)] == [
        "2d", "9b", "1c", "wn", "ws", "dr", "soap", "flower", "joker",
    ]
    # the built deck is already in display order — sorting is a no-op
    deck = build_deck()
    assert sort_rack(deck) == deck


# ── Structural parse (spec §3.1 grammar) ─────────────────────────────────────


def group(count, rank, suit=None):
    g = {"count": count, "rank": rank}
    if suit is not None:
        g["suit"] = suit
    return g


def hand_dict(groups, hand_id="t-1", section="Test Section", name="Test Hand",
              concealed=False, value=30, **overrides):
    h = {"id": hand_id, "section": section, "name": name,
         "concealed": concealed, "value": value, "groups": groups}
    h.update(overrides)
    return h


def card_dict(hands):
    return {"card_id": "test-card", "display_name": "Test Card",
            "season": "2026-test", "hands": hands}


#: A structurally + semantically clean filler hand (sums to 14, has a 3+ group).
def clean_hand(hand_id="t-ok", section="Test Section"):
    return hand_dict(
        [group(4, "F"), group(4, "1", "a"), group(4, "2", "a"), group(2, "3", "a")],
        hand_id=hand_id, section=section,
    )


@pytest.mark.parametrize(
    "bad_group, fragment",
    [
        pytest.param(group(3, "x+6", "a"), "beyond the x..x+5 grammar", id="offset-6"),
        pytest.param(group(3, "x+9", "a"), "beyond the x..x+5 grammar", id="offset-9"),
        pytest.param(group(3, "x-1", "a"), "unknown rank token", id="negative-offset"),
        pytest.param(group(3, "x+0", "a"), "unknown rank token", id="offset-zero-not-grammar"),
        pytest.param(group(3, "x+01", "a"), "unknown rank token", id="offset-leading-zero"),
        pytest.param(group(3, "\u0663", "a"), "unknown rank token", id="arabic-digit"),
        pytest.param(group(3, "\u00b2", "a"), "unknown rank token", id="superscript-digit"),
        pytest.param(group(3, "10", "a"), "unknown rank token", id="rank-10"),
        pytest.param(group(3, "K", "a"), "unknown rank token", id="rank-K"),
        pytest.param(group(3, "", "a"), "rank must be a non-empty string", id="rank-empty"),
        pytest.param(group(3, "5"), "needs a suit letter", id="concrete-no-suit"),
        pytest.param(group(3, "x"), "needs a suit letter", id="var-no-suit"),
        pytest.param(group(3, "N", "a"), "suitless — drop the suit", id="wind-with-suit"),
        pytest.param(group(3, "F", "a"), "suitless — drop the suit", id="flower-with-suit"),
        pytest.param(group(3, "D2", "a"), "suitless — drop the suit", id="dragon2-with-suit"),
        pytest.param(group(3, "0", "a"), "suitless — drop the suit", id="zero-with-suit"),
        pytest.param(group(3, "5", "q"), "suit must be one of a/b/c", id="suit-q"),
        pytest.param(group(0, "5", "a"), "count 0 outside 1–6", id="count-0"),
        pytest.param(group(7, "5", "a"), "count 7 outside 1–6", id="count-7"),
        pytest.param(group(True, "5", "a"), "count must be an integer", id="count-bool"),
        pytest.param(group("3", "5", "a"), "count must be an integer", id="count-str"),
    ],
)
def test_grammar_rejections(bad_group, fragment):
    data = card_dict([hand_dict([bad_group])])
    with pytest.raises(CardError) as exc:
        load_card(data)
    assert any(fragment in p for p in exc.value.problems)


def test_load_reports_every_problem_not_just_the_first():
    data = card_dict([hand_dict([group(3, "K", "a"), group(2, "5")])])
    with pytest.raises(CardError) as exc:
        load_card(data)
    assert len(exc.value.problems) == 2


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        pytest.param({"concealed": "yes"}, "concealed must be true or false", id="concealed-str"),
        pytest.param({"value": "30"}, "value must be an integer", id="value-str"),
        pytest.param({"value": True}, "value must be an integer", id="value-bool"),
        pytest.param({"name": ""}, "name must be a non-empty string", id="name-empty"),
        pytest.param({"section": ""}, "section must be a non-empty string", id="section-empty"),
        pytest.param({"groups": []}, "groups must be a non-empty list", id="groups-empty"),
        pytest.param({"notes": 7}, "notes must be a string", id="notes-int"),
    ],
)
def test_hand_field_rejections(mutate, fragment):
    data = card_dict([{**hand_dict([group(3, "5", "a")]), **mutate}])
    with pytest.raises(CardError) as exc:
        load_card(data)
    assert any(fragment in p for p in exc.value.problems)


def test_card_level_rejections():
    with pytest.raises(CardError):
        load_card([])  # not an object
    with pytest.raises(CardError) as exc:
        load_card({"card_id": "", "display_name": "", "season": "", "hands": []})
    joined = " ".join(exc.value.problems)
    assert "card_id" in joined and "display_name" in joined
    assert "season" in joined and "hands" in joined


def test_zero_rank_parses_suitless_and_lints_clean():
    data = card_dict([
        hand_dict([group(3, "0"), group(4, "F"), group(4, "1", "a"), group(3, "2", "a")],
                  hand_id="z-1"),
        clean_hand("z-2"),
    ])
    card = load_card(data)
    zero_group = card.hands[0].groups[0]
    assert zero_group.kind is RankKind.ZERO
    assert zero_group.suit is None
    assert lint_card(card).errors == []


def test_load_card_file_errors(tmp_path):
    with pytest.raises(CardError, match="cannot read"):
        load_card_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CardError, match="invalid JSON"):
        load_card_file(bad)
    latin = tmp_path / "latin.json"
    latin.write_bytes(b'{"card_id": "caf\xe9"}')  # not UTF-8
    with pytest.raises(CardError, match="cannot read"):
        load_card_file(latin)


# ── Linter (spec §3.4) ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "groups, overrides, fragment",
    [
        pytest.param(
            [group(3, "1", "a"), group(3, "2", "a")], {},
            "sum to 6, not 14", id="sum-under"),
        pytest.param(
            [group(6, "F"), group(6, "1", "a"), group(6, "2", "a")], {},
            "sum to 18, not 14", id="sum-over"),
        pytest.param(
            [group(4, "F"), group(4, "1", "a"), group(4, "2", "a"), group(2, "3", "a")],
            {"value": 20}, "value 20 outside 25–75", id="value-low"),
        pytest.param(
            [group(4, "F"), group(4, "1", "a"), group(4, "2", "a"), group(2, "3", "a")],
            {"value": 80}, "value 80 outside 25–75", id="value-high"),
        pytest.param(
            [group(2, "5", "b"), group(2, "5", "b"), group(2, "5", "b"),
             group(4, "F"), group(4, "1", "a")], {},
            "demand 6 naturals of 5b", id="three-pairs-one-tile"),
        pytest.param(
            [group(2, "N"), group(2, "N"), group(2, "N"),
             group(4, "F"), group(4, "1", "a")], {},
            "demand 6 naturals of N", id="three-pairs-north"),
        pytest.param(
            [group(2, "soap"), group(2, "soap"), group(2, "0"),
             group(4, "x", "a"), group(4, "x+1", "a")], {},
            "demand 6 naturals", id="soap-and-zero-share-a-pool"),
        pytest.param(
            [group(2, "x", "a"), group(2, "x", "a"), group(2, "x", "a"),
             group(4, "F"), group(4, "1", "b")], {},
            "demand 6 naturals of xa", id="three-pairs-same-var"),
        pytest.param(
            [group(2, "D"), group(2, "D"), group(2, "D"),
             group(4, "F"), group(4, "1", "a")], {},
            "demand 6 naturals of D", id="three-pairs-any-dragon"),
        pytest.param(
            [group(6, "F"), group(3, "F"), group(5, "x", "a")], {},
            "demands 9 flowers", id="flowers-over-8"),
        pytest.param(
            [group(2, "R"), group(2, "R"), group(2, "R"),
             group(4, "F"), group(4, "1", "a")], {},
            "demand 6 naturals of R", id="three-pairs-red-dragon"),
        pytest.param(
            [group(6, "1", "a"), group(6, "1", "a"), group(2, "1", "a")], {},
            "needs 10 jokers", id="joker-supply-exceeded"),
        pytest.param(
            [group(2, "F"), group(2, "1", "a"), group(2, "2", "a"), group(2, "3", "a"),
             group(2, "4", "a"), group(2, "5", "a"), group(2, "N")],
            {"concealed": False},
            "no group of 3+ exists to call", id="exposed-all-pairs"),
    ],
)
def test_lint_failures(groups, overrides, fragment):
    report = lint_card_data(card_dict([hand_dict(groups, **overrides)]))
    assert any(fragment in e for e in report.errors), report.errors
    assert not report.ok


@pytest.mark.parametrize(
    "groups, overrides",
    [
        pytest.param(
            [group(2, "5", "b"), group(2, "5", "b"),
             group(4, "F"), group(4, "1", "a"), group(2, "2", "a")], {},
            id="four-naturals-in-pairs-is-legal"),
        pytest.param(
            [group(2, "F"), group(2, "1", "a"), group(2, "2", "a"), group(2, "3", "a"),
             group(2, "4", "a"), group(2, "5", "a"), group(2, "N")],
            {"concealed": True},
            id="concealed-all-pairs-is-legal"),
        pytest.param(
            [group(6, "F"), group(2, "F"), group(4, "1", "a"), group(2, "2", "a")], {},
            id="eight-flowers-is-legal"),
        pytest.param(
            [group(5, "x", "a"), group(4, "x+1", "a"), group(5, "x+2", "a")],
            {"value": 45},
            id="quints-need-jokers-but-3plus-groups-take-them"),
        pytest.param(
            [group(6, "1", "a"), group(6, "1", "a"), group(2, "2", "a")], {},
            id="deficit-of-exactly-8-jokers-is-legal"),
    ],
)
def test_lint_boundary_passes(groups, overrides):
    report = lint_card_data(card_dict([hand_dict(groups, **overrides)]))
    assert report.errors == [], report.errors


def test_duplicate_hand_ids_error():
    report = lint_card_data(card_dict([clean_hand("dup"), clean_hand("dup")]))
    assert any("duplicate hand id 'dup'" in e for e in report.errors)


def test_lint_collects_errors_across_hands():
    report = lint_card_data(card_dict([
        hand_dict([group(3, "1", "a")], hand_id="short"),
        hand_dict(
            [group(4, "F"), group(4, "1", "a"), group(4, "2", "a"), group(2, "3", "a")],
            hand_id="cheap", value=10),
    ]))
    assert len(report.errors) == 2


def test_warning_thin_section():
    report = lint_card_data(card_dict([clean_hand("a-1", section="Lonely")]))
    assert any("section 'Lonely' has only 1 hand" in w for w in report.warnings)
    assert report.ok  # warnings never fail a card


def test_warning_near_duplicate_survives_recolor_and_reorder():
    a = hand_dict(
        [group(3, "1", "a"), group(4, "3", "b"), group(3, "5", "a"), group(4, "7", "b")],
        hand_id="orig")
    b = hand_dict(
        [group(4, "3", "a"), group(3, "1", "b"), group(4, "7", "a"), group(3, "5", "b")],
        hand_id="recolored")
    report = lint_card_data(card_dict([a, b]))
    assert any("near-duplicates" in w and "'orig'" in w and "'recolored'" in w
               for w in report.warnings)


def test_zero_and_soap_spellings_are_near_duplicates():
    a = hand_dict([group(4, "soap"), group(4, "F"), group(4, "1", "a"), group(2, "2", "a")],
                  hand_id="as-soap")
    b = hand_dict([group(4, "0"), group(4, "F"), group(4, "1", "a"), group(2, "2", "a")],
                  hand_id="as-zero")
    report = lint_card_data(card_dict([a, b]))
    assert any("near-duplicates" in w for w in report.warnings)


def test_no_false_duplicate_between_concrete_and_var():
    # qp-1 vs qp-2 shapes: concrete pairs vs a variable run — NOT duplicates
    a = hand_dict(
        [group(2, "F"), group(2, "1", "a"), group(2, "3", "a"), group(2, "5", "a"),
         group(2, "7", "a"), group(2, "9", "a"), group(2, "N")],
        hand_id="concrete", concealed=True)
    b = hand_dict(
        [group(2, "F"), group(2, "x", "a"), group(2, "x+1", "a"), group(2, "x+2", "a"),
         group(2, "x+3", "a"), group(2, "x+4", "a"), group(2, "N")],
        hand_id="variable", concealed=True)
    report = lint_card_data(card_dict([a, b]))
    assert not any("near-duplicates" in w for w in report.warnings)


def test_structural_problems_surface_through_lint_card_data():
    report = lint_card_data(card_dict([hand_dict([group(3, "K", "a")])]))
    assert any("unknown rank token" in e for e in report.errors)
    assert not report.ok


def test_first_light_json_is_valid_json_with_expected_shape():
    data = json.loads(FIRST_LIGHT_PATH.read_text(encoding="utf-8"))
    assert len(data["hands"]) == 22
    report = lint_card_data(data)
    assert report.ok and not report.warnings


def test_non_object_group_and_hand_shapes():
    with pytest.raises(CardError, match="group must be an object"):
        load_card(card_dict([hand_dict(["not-a-group"])]))
    with pytest.raises(CardError, match="hand must be an object"):
        load_card(card_dict(["not-a-hand"]))
    with pytest.raises(CardError, match="id must be a non-empty string"):
        load_card(card_dict([{"section": "S"}]))


def test_landability_belt_fires_if_the_grammar_ever_widens():
    """The x-offset landability check is unreachable through today's x..x+4
    grammar; prove the belt still works by building the shape directly."""
    from bot_modules.games.mahjong.card_logic import Group, Hand, Card

    wide = Group(count=14, rank="x+9", suit="a", kind=RankKind.VAR, offset=9)
    # count=14 keeps the sum rule quiet so the landability error is isolated
    hand = Hand(id="w-1", section="S", name="Wide", concealed=False,
                value=30, groups=(wide,))
    report = lint_card(Card(card_id="c", display_name="C", season="s", hands=(hand,)))
    assert any("no x in 1–9 can carry offset +9" in e for e in report.errors)


def test_tile_labels_and_repr():
    assert Tile.BAM5.label == "5 Bam"
    assert Tile.CRAK1.label == "1 Crak"
    assert Tile.SOAP.label == "Soap"
    assert Tile.JOKER.label == "Joker"
    assert repr(Tile.DOT2) == "Tile<2d>"


# ── Harvest card (grammar v1.1) + the every-hand-winnable invariant ──────────


def _materialize_winning_14(hand):
    """A concrete winning rack for ``hand``: resolve its first binding, take
    naturals up to the supply, jokers for any 3+ shortfall. The linter
    guarantees pairs/singles never outrun their naturals."""
    from collections import Counter

    from bot_modules.games.mahjong.match_logic import (
        _bindings, _group_natural,
    )
    from bot_modules.games.mahjong.tiles import Tile, copies

    x, suit_map, dragons = next(_bindings(hand))
    rack: list[Tile] = []
    supply: Counter = Counter()
    for group in hand.groups:
        natural = _group_natural(group, x, suit_map, dragons)
        available = copies(natural) - supply[natural]
        take = min(group.count, available)
        assert take >= min(group.count, 2), (
            f"{hand.id}: pair/single of {natural} beyond supply")
        supply[natural] += take
        rack += [natural] * take + [Tile.JOKER] * (group.count - take)
    assert len(rack) == 14
    return rack


@pytest.mark.parametrize("card_name", ["first_light", "harvest"])
def test_every_hand_of_every_card_is_winnable(card_name):
    from bot_modules.games.mahjong.card_logic import load_card_file
    from bot_modules.games.mahjong.match_logic import match_hand

    path = FIRST_LIGHT_PATH.parent / f"meadow_{card_name}.json"
    card = load_card_file(path)
    for hand in card.hands:
        rack = _materialize_winning_14(hand)
        matched = {m.hand.id for m in match_hand(rack, [], card)}
        assert hand.id in matched, f"{hand.id} rejects its own winning 14"


def test_harvest_lints_clean_and_loads():
    from bot_modules.games.mahjong.card_logic import (
        lint_card, load_card_file,
    )

    card = load_card_file(FIRST_LIGHT_PATH.parent / "meadow_harvest.json")
    assert len(card.hands) == 71
    report = lint_card(card)
    assert report.errors == []
    # the grammar v1.1 features are all really on the card
    kinds = {g.kind.value for h in card.hands for g in h.groups}
    assert "suit_dragon" in kinds and "any_dragon2" in kinds
    assert any(h.x_parity for h in card.hands)
    assert any(g.offset == 5 for h in card.hands for g in h.groups if g.offset)
