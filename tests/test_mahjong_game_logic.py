"""Tests for the mahjong state machine — stage 3 of docs/plans/meadow-mahjong.md.

Covers spec §10's engine bullets: Charleston routing at both seat counts,
blind passes and the D12 cycle break, the unanimous vote with early no,
courtesy minimums, joker exclusion everywhere, claim arbitration
(Mahjong > call > nearest), private downgrades, joker redemption incl. the
redemption-as-self-pick win, every payout permutation in both modes, wall
game, fallow endings (Duel and D10's 4-seat last-man-standing), the
consecutive-timeout strike rule, rematch, and serialization round-trips at
every phase. Two choreographed hands run deal → Charleston → settle intact.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from bot_modules.games.mahjong import game_logic as G
from bot_modules.games.mahjong.card_logic import load_card, load_first_light
from bot_modules.games.mahjong.mahjong_service import ASSIST_MODES
from bot_modules.games.mahjong.game_logic import (
    ActionRejected,
    GameState,
    Phase,
    RejectCode,
    SeatState,
    TableConfig,
)
from bot_modules.games.mahjong.tiles import Tile, build_deck, shuffled_wall

CARD = load_first_light()


def tiles(spec: str) -> list[Tile]:
    out: list[Tile] = []
    for part in spec.split():
        code, _, n = part.partition("*")
        out.extend([Tile(code)] * (int(n) if n else 1))
    return out


def rng() -> random.Random:
    return random.Random(42)


def stacked_wall(racks: list[list[Tile]], tail: list[Tile] | None = None) -> list[Tile]:
    """A physically consistent 152-wall that deals exactly these racks.

    ``tail`` scripts the post-deal draw order: its FIRST element is the
    first tile drawn (draws pop from the wall's end)."""
    deck = Counter(build_deck())
    for r in racks:
        deck.subtract(Counter(r))
    for t_ in (tail or []):
        deck.subtract([t_])
    assert all(n >= 0 for n in deck.values()), "rack construction exceeds tile copies"
    filler = [t for t, n in deck.items() for _ in range(n)]
    draw_order = list(reversed(tail or []))
    return filler + draw_order + [t for r in reversed(racks) for t in r]


def fresh(seat_count: int, racks: list[list[Tile]] | None = None,
          tail: list[Tile] | None = None, **cfg) -> GameState:
    """A dealt table. With ``racks``, seat i receives exactly racks[i];
    ``tail`` scripts the draw order after the deal."""
    state = G.create_table(TableConfig(seat_count=seat_count, **cfg), 100)
    for m in range(101, 100 + seat_count):
        state, _ = G.join_table(state, m)
    wall = (stacked_wall(racks, tail) if racks
            else shuffled_wall(random.Random(9), cfg.get("max_rank", 9)))
    state, _ = G.deal(state, wall)
    return state


def first_nonjokers(rack: list[Tile], n: int = 3) -> list[Tile]:
    return [t for t in rack if t is not Tile.JOKER][:n]


def advance_to_play(state: GameState) -> GameState:
    """Drive Charleston (junk picks), vote no, courtesy 0 — into PLAY."""
    r = rng()
    for _ in range(3):
        for s in range(len(state.seats)):
            state, _ = G.charleston_pick(
                state, s, first_nonjokers(state.seats[s].rack), 0, r
            )
    if state.phase is Phase.CHARLESTON_VOTE:
        state, _ = G.vote_second_charleston(state, 0, False)
    for s in range(len(state.seats)):
        state, _ = G.courtesy_propose(state, s, 0)
    assert state.phase is Phase.AWAIT_DISCARD
    return state


#: Filler that can always respond to a discard: two jokers is what a *call*
#: needs before the card is consulted, so a seat holding them is never
#: passed for automatically. Cases that need a claim window to stay open for
#: an uninvolved seat ask for this explicitly.
CLAIMABLE_FILLER = "joker*2 9c*11"


def play_state(
    seat_count: int,
    racks: dict[int, str],
    *,
    turn: int = 0,
    wall: str = "1c 1c 1c 1c",
    exposures: dict[int, list[G.ExposureState]] | None = None,
    filler: str = "9c*13",
) -> GameState:
    """Surgical PLAY-phase state; engine transitions deepcopy, so sharing is safe."""
    seats = []
    for i in range(seat_count):
        seats.append(SeatState(
            member_id=100 + i,
            rack=tiles(racks.get(i, filler)),
            exposures=(exposures or {}).get(i, []),
        ))
    return GameState(
        config=TableConfig(seat_count=seat_count),
        host=100,
        seats=seats,
        phase=Phase.AWAIT_DISCARD,
        turn=turn,
        hand_no=1,
        wall=tiles(wall),
        next_exposure_id=10,
    )


def total_tiles(state: GameState) -> int:
    n = len(state.wall) + len(state.discards)
    for s in state.seats:
        n += len(s.rack) + sum(e.count for e in s.exposures)
    return n


# ── Lobby & guards ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("seat_count", [2, 4])
def test_lobby_fills_and_deals(seat_count):
    state = G.create_table(TableConfig(seat_count=seat_count), 100)
    with pytest.raises(ActionRejected) as e:
        G.deal(state, shuffled_wall(rng()))
    assert e.value.code is RejectCode.NOT_ENOUGH_SEATS
    for m in range(101, 100 + seat_count):
        state, ev = G.join_table(state, m)
    assert ("table_full", {}) in ev
    with pytest.raises(ActionRejected) as e:
        G.join_table(state, 999)
    assert e.value.code is RejectCode.TABLE_FULL
    with pytest.raises(ActionRejected) as e:
        G.join_table(state, 100)
    assert e.value.code is RejectCode.ALREADY_SEATED

    state, _ = G.deal(state, shuffled_wall(rng()))
    assert state.phase is Phase.CHARLESTON
    for i, s in enumerate(state.seats):
        assert len(s.rack) == (14 if i == state.dealer else 13)
    assert total_tiles(state) == 152


def test_cancel_host_only():
    state = G.create_table(TableConfig(seat_count=2), 100)
    state, _ = G.join_table(state, 101)
    with pytest.raises(ActionRejected) as e:
        G.cancel_table(state, 101)
    assert e.value.code is RejectCode.NOT_HOST
    closed, ev = G.cancel_table(state, 100)
    assert closed.phase is Phase.CLOSED
    modded, _ = G.cancel_table(state, None)  # mod/service cancel
    assert modded.phase is Phase.CLOSED


def test_cancel_only_in_lobby():
    state = fresh(2)
    with pytest.raises(ActionRejected) as e:
        G.cancel_table(state, 100)
    assert e.value.code is RejectCode.WRONG_PHASE


# ── Short decks (TableConfig.max_rank) ───────────────────────────────────────


@pytest.mark.parametrize("max_rank, size", [(9, 152), (6, 116), (5, 104)])
def test_the_deck_shrinks_with_the_rank_ceiling(max_rank, size):
    from bot_modules.games.mahjong.tiles import build_deck as bd, deck_size
    assert deck_size(max_rank) == size
    deck = bd(max_rank)
    assert len(deck) == size
    assert max((t.rank or 0) for t in deck if t.is_suited) == max_rank
    # honours, flowers and jokers are never trimmed
    assert sum(1 for t in deck if t is Tile.JOKER) == 8
    assert sum(1 for t in deck if t.is_wind) == 16


def test_a_trimmed_tile_reports_no_copies():
    """The load-bearing one. Reachability counts *unseen copies* to judge
    whether a line is still live, so a tile that is not in the deck must
    report zero — otherwise the matcher, the assist engine and the bots all
    keep believing in tiles nobody can draw."""
    from bot_modules.games.mahjong.tiles import copies, copies_in_play
    assert copies(Tile("9d")) == 4               # the tile exists as a kind
    assert copies_in_play(Tile("9d"), 5) == 0    # but not on this table
    assert copies_in_play(Tile("5d"), 5) == 4
    assert copies_in_play(Tile.JOKER, 5) == 8


@pytest.mark.parametrize("bad", [4, 10, 0, -1])
def test_a_ceiling_outside_the_supported_range_is_refused(bad):
    with pytest.raises(ValueError, match="max_rank"):
        TableConfig(seat_count=4, max_rank=bad)


def test_the_ceiling_survives_persistence_and_defaults_for_old_tables():
    state = fresh(4, max_rank=6)
    assert state.config.max_rank == 6
    assert G.state_from_dict(G.state_to_dict(state)).config.max_rank == 6
    # a table dealt before the option existed played the full game
    data = G.state_to_dict(state)
    del data["config"]["max_rank"]
    assert G.state_from_dict(data).config.max_rank == 9


def test_a_short_deck_deals_a_shorter_wall():
    full = fresh(4)
    short = fresh(4, max_rank=5)
    assert len(short.wall) < len(full.wall)
    assert len(short.wall) == 104 - (14 + 13 * 3)


def test_no_line_stays_reachable_on_tiles_the_deck_lacks():
    """The bug this plumbing exists to prevent: without a deck-aware copy
    count, a line wanting 9s reads as live on a deck that stops at 5."""
    from bot_modules.games.mahjong.match_logic import reachable_lines

    card = load_card({
        "card_id": "t", "display_name": "t", "season": "t",
        "hands": [{"id": "highs", "section": "S", "name": "Highs",
                   "concealed": False, "value": 25,
                   "groups": [{"count": 4, "rank": "9", "suit": "a"},
                              {"count": 4, "rank": "8", "suit": "a"},
                              {"count": 3, "rank": "7", "suit": "a"},
                              {"count": 3, "rank": "F"}]}],
    })
    # Nothing high is held: on the full deck the 3+ groups are joker- and
    # draw-fillable, on the short deck neither route exists.
    rack = tiles("1d*13")
    assert [h.id for h in reachable_lines(rack, [], card, Counter(), 9)] == ["highs"]
    assert reachable_lines(rack, [], card, Counter(), 5) == []


def test_a_run_never_binds_past_the_ceiling():
    from bot_modules.games.mahjong.match_logic import _bindings

    hand = load_card({
        "card_id": "t", "display_name": "t", "season": "t",
        "hands": [{"id": "run", "section": "S", "name": "Run",
                   "concealed": False, "value": 25,
                   "groups": [{"count": 4, "rank": "x", "suit": "a"},
                              {"count": 4, "rank": "x+1", "suit": "a"},
                              {"count": 3, "rank": "x+2", "suit": "a"},
                              {"count": 3, "rank": "F"}]}],
    }).hands[0]
    assert max(x for x, _, _ in _bindings(hand, 9)) == 7      # 7,8,9
    assert max(x for x, _, _ in _bindings(hand, 5)) == 3      # 3,4,5


def test_a_wall_that_does_not_match_the_table_is_refused():
    """The engine trusts its caller for the wall, so this is the only place
    a full deck dealt to a short table would ever be noticed."""
    state = G.create_table(TableConfig(seat_count=4, max_rank=5), 100)
    for m in (101, 102, 103):
        state, _ = G.join_table(state, m)
    with pytest.raises(ValueError, match="rank ceiling"):
        G.deal(state, shuffled_wall(rng()))            # full 152-tile wall
    state, _ = G.deal(state, shuffled_wall(rng(), 5))  # the right one
    assert state.phase is Phase.CHARLESTON


# ── Auto-pass: seats with no legal route (§2.5) ──────────────────────────────


def _window_for(rack0: str, discard: str, seat_count: int = 2):
    state = play_state(seat_count, {0: rack0, 1: f"9c*12 {discard}"}, turn=1)
    return G.discard(state, 1, Tile(discard), CARD)[0]


def _open_window_for(rack0: str, discard: str):
    """A four-seat window that stays open after seat 0 responds: seat 2 holds
    two of the discard, so it is a live responder and nothing resolves."""
    state = play_state(
        4, {0: rack0, 1: f"9c*12 {discard}", 2: f"{discard}*2 9d*11"}, turn=1
    )
    return G.discard(state, 1, Tile(discard), CARD)[0]


def test_a_seat_with_no_legal_route_is_passed_for():
    """The measured waste: over 964 windows only 31.5% of responder-slots
    could act at all. Holding the table for a decision that does not exist
    is the single largest avoidable delay in the game."""
    state = _window_for("flower*4 1d*4 6b*3 8c 9b", "5c")
    assert state.claims[0] == (G.AUTO_PASS, [])
    assert state.phase is Phase.CLAIM_WINDOW  # the window still opens


@pytest.mark.parametrize(
    "rack0, discard",
    [
        pytest.param("flower*4 1d*4 6b*2 8c*2 9b", "6b", id="two-naturals"),
        pytest.param("flower*4 1d*4 6b joker 8c*2 9b", "6b", id="natural-plus-joker"),
        pytest.param("flower*4 2d*4 6b*4 8c", "8c", id="instant-mahjong"),
    ],
)
def test_a_seat_with_any_legal_route_is_left_to_choose(rack0, discard):
    """Generous on purpose: two matching tiles is what a call needs before
    the card is even consulted, and an instant Mahjong is the other route.
    Passing for a seat that had one would take a real decision away."""
    assert 0 not in _window_for(rack0, discard).claims


def test_a_discarded_joker_passes_the_whole_table():
    """Claimable by nobody, ever — so nobody should be waited on."""
    state = _window_for("flower*4 1d*4 6b*3 joker*2", "joker", seat_count=4)
    assert all(state.claims[s] == (G.AUTO_PASS, []) for s in (0, 2, 3))


def test_auto_pass_alone_does_not_resolve_the_window():
    """The window is also the beat in which a table reads what was thrown;
    the service arms a short deadline rather than collapsing it to zero."""
    state = _window_for("flower*4 1d*4 6b*3 8c 9b", "5c")
    assert state.phase is Phase.CLAIM_WINDOW
    assert state.live_discard == Tile("5c")


def test_a_seat_passed_for_may_still_tap_without_being_told_off():
    """Its panel may still show the buttons. Being told "you've already
    responded" for a choice the engine made on your behalf reads as a bug."""
    state = _open_window_for("flower*4 1d*4 6b*3 8c 9b", "5c")
    assert state.claims[0] == (G.AUTO_PASS, [])
    state, _ = G.claim(state, 0, "pass", [], CARD, rng())  # must not raise
    assert state.claims[0] == ("pass", [])


def test_a_seat_passed_for_that_taps_call_still_fails_privately():
    """§2.5's privacy rule outlives the shortcut: an illegal claim converts
    to a pass without a visible error, and must not become one just because
    the engine got there first."""
    state = _open_window_for("flower*4 1d*4 6b*3 8c 9b", "5c")
    state, ev = G.claim(state, 0, "call", tiles("9b*2"), CARD, rng())
    assert any(k == "claim_downgraded" and d.get("private") for k, d in ev)
    assert state.claims[0] == ("pass", [])


def test_a_seat_that_chose_still_cannot_choose_again():
    state = _open_window_for("flower*4 1d*4 6b*2 8c*2 9b", "6b")
    assert 0 not in state.claims  # it had a route, so it was left to choose
    state, _ = G.claim(state, 0, "pass", [], CARD, rng())
    with pytest.raises(ActionRejected) as e:
        G.claim(state, 0, "call", tiles("6b*2"), CARD, rng())
    assert e.value.code is RejectCode.ALREADY_SUBMITTED


def test_being_passed_for_counts_as_renouncing_the_tile():
    """`obtainable_seen` stops discounting a discard once a seat has given
    it up; a seat that never had a route has given it up too."""
    state = _window_for("flower*4 1d*4 6b*3 8c 9b", "5c")
    raw = G.seen_elsewhere(state, 0)
    adjusted = G.obtainable_seen(state, 0, CARD)
    assert adjusted.get(Tile("5c"), 0) == raw[Tile("5c")]


def test_discard_count_counts_turns_not_the_pit():
    """A called tile is popped back off the pit, so `len(discards)` is not
    the number of turns taken — and turns are what cost a table its clock.

    This is also why a wall game's pit always ends at wall size plus one
    however many calls happened: a call adds a turn and removes a pit tile,
    and the two cancel exactly.
    """
    state = play_state(4, {0: "1d*13"})
    assert state.discard_count == 0
    state, _ = G.discard(state, 0, Tile("1d"), CARD)
    assert state.discard_count == 1 and len(state.discards) == 1

    # a claim consumes the discard; the turn still happened
    state.discards.pop()
    assert state.discard_count == 1 and len(state.discards) == 0

    # and it survives persistence
    assert G.state_from_dict(G.state_to_dict(state)).discard_count == 1


def test_a_fresh_deal_resets_the_turn_counter():
    """Rematches deal into a state that already played a hand, so the deal
    has to zero this or every later hand inherits the last one's count."""
    state = fresh(4)
    assert state.discard_count == 0
    state.discard_count = 37  # as if a hand had just been played
    state.phase = Phase.SETTLE
    state.rematch_votes = {0, 1, 2, 3}
    state, _ = G.deal(state, shuffled_wall(rng()))
    assert state.discard_count == 0


def test_wall_trim_shortens_the_wall():
    plain = fresh(2)
    trimmed = fresh(2, wall_trim=60)
    assert len(plain.wall) == 152 - 27
    assert len(trimmed.wall) == 152 - 27 - 60
    assert total_tiles(trimmed) == 92


def test_wrong_phase_actions_reject():
    state = G.create_table(TableConfig(seat_count=2), 100)
    for fn in (
        lambda: G.discard(state, 0, Tile.NORTH, CARD),
        lambda: G.charleston_pick(state, 0, [], 0, rng()),
        lambda: G.vote_second_charleston(state, 0, True),
        lambda: G.courtesy_propose(state, 0, 0),
        lambda: G.courtesy_pick(state, 0, []),
        lambda: G.claim(state, 0, "pass", [], CARD, rng()),
        lambda: G.redeem_joker(state, 0, 1, Tile.NORTH),
        lambda: G.declare_mahjong_own_turn(state, 0, CARD),
        lambda: G.rematch_vote(state, 0),
    ):
        with pytest.raises(ActionRejected) as e:
            fn()
        assert e.value.code is RejectCode.WRONG_PHASE


# ── Charleston (§2.3) ────────────────────────────────────────────────────────


def test_charleston_routing_4seat_right_across_left():
    state = fresh(4)
    markers = {s: first_nonjokers(state.seats[s].rack) for s in range(4)}
    for s in range(4):
        state, _ = G.charleston_pick(state, s, markers[s], 0, rng())
    # pass 1 goes right: seat s's tiles landed with seat s+1
    for s in range(4):
        rack = Counter(state.seats[(s + 1) % 4].rack)
        assert all(rack[t] >= n for t, n in Counter(markers[s]).items())

    markers = {s: first_nonjokers(state.seats[s].rack) for s in range(4)}
    for s in range(4):
        state, _ = G.charleston_pick(state, s, markers[s], 0, rng())
    for s in range(4):  # pass 2: across
        rack = Counter(state.seats[(s + 2) % 4].rack)
        assert all(rack[t] >= n for t, n in Counter(markers[s]).items())

    markers = {s: first_nonjokers(state.seats[s].rack) for s in range(4)}
    for s in range(4):
        state, _ = G.charleston_pick(state, s, markers[s], 0, rng())
    for s in range(4):  # pass 3: left
        rack = Counter(state.seats[(s + 3) % 4].rack)
        assert all(rack[t] >= n for t, n in Counter(markers[s]).items())
    assert state.phase is Phase.CHARLESTON_VOTE


def test_charleston_routing_duel_always_opponent():
    state = fresh(2)
    for _ in range(3):
        markers = {s: first_nonjokers(state.seats[s].rack) for s in range(2)}
        for s in range(2):
            state, _ = G.charleston_pick(state, s, markers[s], 0, rng())
        for s in range(2):
            rack = Counter(state.seats[1 - s].rack)
            assert all(rack[t] >= n for t, n in Counter(markers[s]).items())
    assert state.phase is Phase.CHARLESTON_VOTE


def test_charleston_guards():
    state = fresh(2)
    r = rng()
    with pytest.raises(ActionRejected) as e:  # jokers never pass
        G.charleston_pick(state, 0, [Tile.JOKER] + first_nonjokers(state.seats[0].rack, 2), 0, r)
    assert e.value.code is RejectCode.JOKERS_NEVER_PASS
    with pytest.raises(ActionRejected) as e:  # wrong count
        G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack, 2), 0, r)
    assert e.value.code is RejectCode.BAD_COUNT
    with pytest.raises(ActionRejected) as e:  # blind only on final pass
        G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack, 2), 1, r)
    assert e.value.code is RejectCode.BLIND_NOT_NOW
    with pytest.raises(ActionRejected) as e:  # not holding those tiles
        missing = [t for t in Tile if t not in state.seats[0].rack][0]
        G.charleston_pick(state, 0, [missing] * 3, 0, r)
    assert e.value.code is RejectCode.TILE_NOT_IN_RACK
    state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
    with pytest.raises(ActionRejected) as e:  # double submit
        G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
    assert e.value.code is RejectCode.ALREADY_SUBMITTED


def test_blind_pass_forwards_unseen_from_the_feeder():
    state = fresh(4)
    r = rng()
    for _ in range(2):  # reach the final pass
        for s in range(4):
            state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
    # seat 1 blinds 2: its lane = 1 own pick + first 2 of seat 0's lane (feeder
    # for the left... pass 3 is LEFT (+3): seat 1 receives from seat 2)
    feeder_pick = first_nonjokers(state.seats[2].rack)
    own_pick = first_nonjokers(state.seats[1].rack, 1)
    state, _ = G.charleston_pick(state, 2, feeder_pick, 0, r)
    state, _ = G.charleston_pick(state, 1, own_pick, 2, r)
    state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
    state, ev = G.charleston_pick(state, 3, first_nonjokers(state.seats[3].rack), 0, r)
    assert any(k == "pass_resolved" for k, _ in ev)
    # seat 1's lane went to seat (1+3)%4 = 0: own pick + first 2 of feeder's
    rack0 = Counter(state.seats[0].rack)
    for t in own_pick + feeder_pick[:2]:
        assert rack0[t] >= 1
    assert state.phase is Phase.CHARLESTON_VOTE
    assert all(len(s.rack) == (14 if i == state.dealer else 13)
               for i, s in enumerate(state.seats))


def test_blind_cycle_breaks_privately_and_moves_no_jokers():
    # Duel, both blind all 3 on the final pass: the D12 rack-random break
    state = fresh(2)
    r = rng()
    for _ in range(2):
        for s in range(2):
            state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
    jokers_before = [sum(1 for t in s.rack if t is Tile.JOKER) for s in state.seats]
    state, _ = G.charleston_pick(state, 0, [], 3, r)
    state, ev = G.charleston_pick(state, 1, [], 3, r)
    assert any(k == "pass_resolved" for k, _ in ev)
    jokers_after = [sum(1 for t in s.rack if t is Tile.JOKER) for s in state.seats]
    assert jokers_before == jokers_after  # jokers can never be passed
    assert len(state.seats[0].rack) == 14 and len(state.seats[1].rack) == 13
    assert total_tiles(state) == 152


def test_charleston_timeout_autopasses_and_strikes():
    state = fresh(4)
    r = rng()
    state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
    state, ev = G.timeout(state, CARD, r)
    kinds = [k for k, _ in ev]
    assert kinds.count("strike") == 3  # seats 1..3 auto-resolved
    assert any(k == "pass_resolved" for k, _ in ev)
    assert state.seats[0].strikes == 0
    assert all(state.seats[s].strikes == 1 for s in (1, 2, 3))
    assert state.pass_index == 1


# ── Vote & second Charleston (§2.3.2) ────────────────────────────────────────


def run_first_charleston(state: GameState) -> GameState:
    r = rng()
    for _ in range(3):
        for s in range(len(state.seats)):
            state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
    return state


def test_unanimous_yes_runs_second_charleston_left_across_right():
    state = run_first_charleston(fresh(4))
    for s in range(4):
        state, ev = G.vote_second_charleston(state, s, True)
    assert ("vote_resolved", {"passed": True}) in ev
    assert state.phase is Phase.CHARLESTON and state.charleston_round == 2
    # pass 1 of round 2 goes LEFT (+3)
    markers = {s: first_nonjokers(state.seats[s].rack) for s in range(4)}
    r = rng()
    for s in range(4):
        state, _ = G.charleston_pick(state, s, markers[s], 0, r)
    for s in range(4):
        rack = Counter(state.seats[(s + 3) % 4].rack)
        assert all(rack[t] >= n for t, n in Counter(markers[s]).items())


def test_any_no_resolves_vote_early():
    state = run_first_charleston(fresh(4))
    state, _ = G.vote_second_charleston(state, 1, True)
    state, ev = G.vote_second_charleston(state, 3, False)
    assert ("vote_resolved", {"passed": False}) in ev
    assert state.phase is Phase.COURTESY_PROPOSE
    with pytest.raises(ActionRejected):  # nothing left to vote on
        G.vote_second_charleston(state, 0, True)


def test_vote_timeout_counts_missing_as_no_with_strikes():
    state = run_first_charleston(fresh(4))
    state, _ = G.vote_second_charleston(state, 0, True)
    state, ev = G.timeout(state, CARD, rng())
    assert ("vote_resolved", {"passed": False}) in ev
    assert state.phase is Phase.COURTESY_PROPOSE
    assert [s.strikes for s in state.seats] == [0, 1, 1, 1]


def test_second_charleston_dashboard_lever_off_skips_the_vote():
    state = run_first_charleston(fresh(2, second_charleston=False))
    assert state.phase is Phase.COURTESY_PROPOSE


# ── Courtesy (§2.3.3) ────────────────────────────────────────────────────────


def to_courtesy(state: GameState) -> GameState:
    state = run_first_charleston(state)
    if state.phase is Phase.CHARLESTON_VOTE:
        state, _ = G.vote_second_charleston(state, 0, False)
    return state


def test_courtesy_minimum_of_the_pair():
    state = to_courtesy(fresh(4))
    for s, n in ((0, 3), (1, 0), (2, 1), (3, 2)):
        state, _ = G.courtesy_propose(state, s, n)
    # pairs (0,2): min(3,1)=1 each; (1,3): min(0,2)=0
    assert state.phase is Phase.COURTESY_PICK
    assert state.courtesy_owed == {0: 1, 2: 1}
    give0 = first_nonjokers(state.seats[0].rack, 1)
    give2 = first_nonjokers(state.seats[2].rack, 1)
    state, _ = G.courtesy_pick(state, 0, give0)
    state, _ = G.courtesy_pick(state, 2, give2)
    assert state.phase is Phase.AWAIT_DISCARD
    assert give0[0] in state.seats[2].rack
    assert give2[0] in state.seats[0].rack
    assert total_tiles(state) == 152


def test_courtesy_all_zero_goes_straight_to_play():
    state = to_courtesy(fresh(2))
    state, _ = G.courtesy_propose(state, 0, 0)
    state, ev = G.courtesy_propose(state, 1, 0)
    assert state.phase is Phase.AWAIT_DISCARD
    assert state.turn == state.dealer
    assert any(k == "play_begins" for k, _ in ev)


def test_courtesy_guards():
    state = to_courtesy(fresh(2))
    with pytest.raises(ActionRejected) as e:
        G.courtesy_propose(state, 0, 4)
    assert e.value.code is RejectCode.BAD_COUNT
    state, _ = G.courtesy_propose(state, 0, 2)
    with pytest.raises(ActionRejected) as e:
        G.courtesy_propose(state, 0, 1)
    assert e.value.code is RejectCode.ALREADY_SUBMITTED
    state, _ = G.courtesy_propose(state, 1, 3)
    assert state.courtesy_owed == {0: 2, 1: 2}
    with pytest.raises(ActionRejected) as e:  # joker in the give
        G.courtesy_pick(state, 0, [Tile.JOKER, Tile.JOKER])
    assert e.value.code is RejectCode.JOKERS_NEVER_PASS
    with pytest.raises(ActionRejected) as e:  # wrong count
        G.courtesy_pick(state, 0, first_nonjokers(state.seats[0].rack, 1))
    assert e.value.code is RejectCode.BAD_COUNT


def test_courtesy_timeouts_auto_resolve():
    state = to_courtesy(fresh(2))
    state, _ = G.courtesy_propose(state, 0, 2)
    state, ev = G.timeout(state, CARD, rng())  # seat 1 proposes 0
    assert state.phase is Phase.AWAIT_DISCARD  # min(2, 0) = 0 — nothing owed
    assert state.seats[1].strikes == 1

    state = to_courtesy(fresh(2))
    state, _ = G.courtesy_propose(state, 0, 1)
    state, _ = G.courtesy_propose(state, 1, 1)
    state, _ = G.courtesy_pick(state, 0, first_nonjokers(state.seats[0].rack, 1))
    state, ev = G.timeout(state, CARD, rng())  # seat 1's give is random
    assert state.phase is Phase.AWAIT_DISCARD
    assert state.seats[1].strikes == 1
    assert total_tiles(state) == 152


# ── Turn loop & claims (§2.4–§2.5) ───────────────────────────────────────────


def open_window(state: GameState, seat: int, tile: Tile):
    state, _ = G.discard(state, seat, tile, CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    return state


def test_discard_guards():
    state = play_state(2, {0: "1c*14", 1: "9d*13"})
    with pytest.raises(ActionRejected) as e:
        G.discard(state, 1, Tile("9d"), CARD)
    assert e.value.code is RejectCode.NOT_YOUR_TURN
    with pytest.raises(ActionRejected) as e:
        G.discard(state, 0, Tile("5b"), CARD)
    assert e.value.code is RejectCode.TILE_NOT_IN_RACK


def test_all_pass_draws_for_the_next_seat():
    state = play_state(4, {0: "1c*14"}, wall="5d 6d 7d", filler=CLAIMABLE_FILLER)
    state = open_window(state, 0, Tile("1c"))
    r = rng()
    for s in (1, 2):
        state, _ = G.claim(state, s, "pass", [], CARD, r)
    state, ev = G.claim(state, 3, "pass", [], CARD, r)
    assert any(k == "tile_drawn" for k, _ in ev)
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 1
    assert state.drawn is Tile("7d")  # wall pops from the end
    assert len(state.seats[1].rack) == 14
    assert state.discards == [(0, Tile("1c"))]  # a passed discard stays in the pit


def test_duel_single_respondent_window():
    state = play_state(2, {0: "1c*14", 1: "9d*13"}, wall="2b")
    state = open_window(state, 0, Tile("1c"))
    state, ev = G.claim(state, 1, "pass", [], CARD, rng())
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 1
    assert state.drawn is Tile("2b")


def test_claim_window_guards():
    state = play_state(2, {0: "1c*14", 1: "9d*13"})
    state = open_window(state, 0, Tile("1c"))
    with pytest.raises(ActionRejected) as e:  # your own discard
        G.claim(state, 0, "pass", [], CARD, rng())
    assert e.value.code is RejectCode.NOT_YOUR_TURN
    state, _ = G.claim(state, 1, "pass", [], CARD, rng())
    with pytest.raises(ActionRejected) as e:  # window closed
        G.claim(state, 1, "pass", [], CARD, rng())
    assert e.value.code is RejectCode.WRONG_PHASE


def test_call_makes_an_exposure_and_turn_continues_right():
    r = rng()
    state = play_state(4, {0: "5b 1c*13", 2: "5b*2 9d*11"}, wall="2b 3b 4b", filler=CLAIMABLE_FILLER)
    state = open_window(state, 0, Tile("5b"))
    state, _ = G.claim(state, 1, "pass", [], CARD, r)
    state, _ = G.claim(state, 3, "pass", [], CARD, r)
    state, ev = G.claim(state, 2, "call", tiles("5b*2"), CARD, r)
    assert any(k == "exposure_made" for k, _ in ev)
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 2
    exp = state.seats[2].exposures[0]
    assert (exp.natural, exp.count, exp.jokers) == (Tile("5b"), 3, 0)
    assert state.discards == []  # claimed tile left the pit
    assert len(state.seats[2].rack) == 11  # 13 - 2 exposed
    # claimant discards; on all-pass, play continues to the claimant's right
    state, _ = G.discard(state, 2, Tile("9d"), CARD)
    for s in (0, 1):
        state, _ = G.claim(state, s, "pass", [], CARD, rng())
    state, _ = G.claim(state, 3, "pass", [], CARD, rng())
    assert state.turn == 3


def test_call_with_jokers_counts_them():
    state = play_state(2, {0: "5b 1c*13", 1: "5b joker 9d*11"})
    state = open_window(state, 0, Tile("5b"))
    state, ev = G.claim(state, 1, "call", [Tile("5b"), Tile.JOKER], CARD, rng())
    exp = state.seats[1].exposures[0]
    assert (exp.natural, exp.count, exp.jokers) == (Tile("5b"), 3, 1)


@pytest.mark.parametrize(
    "rack, call_tiles, why",
    [
        pytest.param("5b 9d*12", "5b", "pair_call", id="pair-call"),
        pytest.param("9d*13", "5b*2", "not_in_rack", id="tiles-not-held"),
        pytest.param("5b 2c 9d*11", "5b 2c", "no_matching_group", id="mismatched-naturals"),
        pytest.param("5b*4 joker*2 9d*7", "5b*4 joker*2", "too_big", id="beyond-sextet"),
    ],
)
def test_illegal_calls_downgrade_privately(rack, call_tiles, why):
    state = play_state(2, {0: "5b 1c*13", 1: rack}, wall="2b")
    state = open_window(state, 0, Tile("5b"))
    state, ev = G.claim(state, 1, "call", tiles(call_tiles), CARD, rng())
    assert ("claim_downgraded", {"seat": 1, "why": why, "private": True}) in ev
    # the downgrade resolves as a pass: window closes, seat 1 just draws
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 1


def test_joker_discard_can_never_be_claimed():
    state = play_state(2, {0: "joker 1c*13", 1: "joker*2 9d*11"}, wall="2b")
    state = open_window(state, 0, Tile.JOKER)
    state, ev = G.claim(state, 1, "call", tiles("joker*2"), CARD, rng())
    assert ("claim_downgraded", {"seat": 1, "why": "joker_discard", "private": True}) in ev

    state = play_state(2, {0: "joker 1c*13", 1: "flower*4 2d*4 6b*4 8c"}, wall="2b")
    state = open_window(state, 0, Tile.JOKER)
    state, ev = G.claim(state, 1, "mahjong", [], CARD, rng())
    assert ("claim_downgraded", {"seat": 1, "why": "invalid_mahjong", "private": True}) in ev


def test_mahjong_claim_wins_and_reveals():
    # seat 1 holds gh-1 minus one 8c; seat 0 throws it
    state = play_state(2, {0: "8c 1c*13", 1: "flower*4 2d*4 6b*4 8c"})
    state = open_window(state, 0, Tile("8c"))
    state, ev = G.claim(state, 1, "mahjong", [], CARD, rng())
    kinds = [k for k, _ in ev]
    assert "mahjong" in kinds and "hand_settled" in kinds
    assert state.phase is Phase.SETTLE
    out = state.outcome
    assert out is not None and out.kind == "mahjong"
    assert out.winner == 1 and out.line_id == "gh-1" and out.won_by == "discard"
    assert out.discarder == 0
    assert state.discards == []  # the winning tile leaves the pit


def test_invalid_mahjong_claim_fails_privately_and_window_continues():
    state = play_state(4, {0: "8c 1c*13", 1: "9d*13"}, filler=CLAIMABLE_FILLER)
    state = open_window(state, 0, Tile("8c"))
    state, ev = G.claim(state, 1, "mahjong", [], CARD, rng())
    assert ("claim_downgraded", {"seat": 1, "why": "invalid_mahjong", "private": True}) in ev
    assert state.phase is Phase.CLAIM_WINDOW  # two seats still to respond
    assert state.claims[1] == ("pass", [])


def test_concealed_line_can_win_by_discard_claim_but_not_with_exposures():
    # qp-3 Echo (concealed): 2(1)a 2(2)a 2(3)a 2(1)b 2(2)b 2(3)b 2(R)
    rack = "1d*2 2d*2 3d*2 1b*2 2b*2 3b*2 dr"
    state = play_state(2, {0: "dr 1c*13", 1: rack})
    state = open_window(state, 0, Tile("dr"))
    state, ev = G.claim(state, 1, "mahjong", [], CARD, rng())
    assert state.outcome is not None and state.outcome.line_id == "qp-3"

    # same 13 but the seat has an exposure: the concealed line is dead
    state = play_state(
        2, {0: "dr 1c*13", 1: rack},
        exposures={1: [G.ExposureState(1, Tile("9c"), 3, 0)]},
    )
    state = open_window(state, 0, Tile("dr"))
    state, ev = G.claim(state, 1, "mahjong", [], CARD, rng())
    assert ("claim_downgraded", {"seat": 1, "why": "invalid_mahjong", "private": True}) in ev


def test_arbitration_mahjong_beats_call_beats_nearest():
    # seat 3 wants the 8c for a call; seat 2 completes gh-1 with it
    state = play_state(
        4,
        {0: "8c 1c*13", 2: "flower*4 2d*4 6b*4 8c", 3: "8c*2 9d*11"},
    )
    state = open_window(state, 0, Tile("8c"))
    r = rng()
    state, _ = G.claim(state, 3, "call", tiles("8c*2"), CARD, r)
    state, _ = G.claim(state, 1, "pass", [], CARD, r)
    state, ev = G.claim(state, 2, "mahjong", [], CARD, r)
    assert state.outcome is not None and state.outcome.winner == 2

    # two calls: nearest in turn order from the discarder wins
    state = play_state(4, {1: "5b 1c*13", 2: "5b*2 9d*11", 0: "5b*2 9d*11"})
    state.turn = 1
    state = open_window(state, 1, Tile("5b"))
    r = rng()
    state, _ = G.claim(state, 0, "call", tiles("5b*2"), CARD, r)
    state, _ = G.claim(state, 3, "pass", [], CARD, r)
    state, ev = G.claim(state, 2, "call", tiles("5b*2"), CARD, r)
    # from discarder 1: seat 2 is nearest (distance 1) vs seat 0 (distance 3)
    assert state.turn == 2 and state.seats[2].exposures


def test_two_mahjong_claims_nearest_wins():
    state = play_state(
        4,
        {0: "8c 1c*13",
         1: "flower*4 2d*4 6b*4 8c",          # gh-1 via the claim
         3: "2c*4 4c*3 6c*3 8c flower*2"},    # eg-1 in craks via the claim
    )
    state = open_window(state, 0, Tile("8c"))
    r = rng()
    state, _ = G.claim(state, 3, "mahjong", [], CARD, r)
    state, _ = G.claim(state, 2, "pass", [], CARD, r)
    state, ev = G.claim(state, 1, "mahjong", [], CARD, r)
    assert state.outcome is not None
    assert state.outcome.winner == 1 and state.outcome.line_id == "gh-1"


def test_claim_window_timeout_resolves_with_standing_claims():
    state = play_state(4, {0: "5b 1c*13", 2: "5b*2 9d*11"}, wall="2b", filler=CLAIMABLE_FILLER)
    state = open_window(state, 0, Tile("5b"))
    r = rng()
    state, _ = G.claim(state, 2, "call", tiles("5b*2"), CARD, r)
    state, ev = G.timeout(state, CARD, r)   # 1 and 3 never respond
    assert state.turn == 2 and state.seats[2].exposures
    # silence in the window is a normal pass — no strikes for it
    assert all(s.strikes == 0 for s in state.seats)


# ── Turn timeouts, strikes, fallow (§2.4, §6.2) ──────────────────────────────


def test_turn_timeout_autodiscards_the_drawn_tile():
    state = play_state(2, {0: "1c*14", 1: "9d*13"}, wall="2b 3b")
    state = open_window(state, 0, Tile("1c"))
    state, _ = G.claim(state, 1, "pass", [], CARD, rng())
    assert state.drawn is Tile("3b")
    state, ev = G.timeout(state, CARD, rng())
    assert ("discard_placed", {"seat": 1, "tile": "3b"}) in ev
    assert state.seats[1].strikes == 1
    assert state.phase is Phase.CLAIM_WINDOW


def test_dealer_first_turn_timeout_discards_a_random_nonjoker():
    state = play_state(2, {0: "joker*3 1c*11", 1: "9d*13"})
    state.drawn = None
    state, ev = G.timeout(state, CARD, rng())
    placed = next(d for k, d in ev if k == "discard_placed")
    assert placed["tile"] != "joker"


def test_three_consecutive_timeouts_fold_a_seat_and_timely_acts_reset():
    state = play_state(4, {0: "1c*14"}, wall="9d*40")
    r = rng()

    def timeout_turn(state, seat):
        assert state.phase is Phase.AWAIT_DISCARD and state.turn == seat
        state, ev = G.timeout(state, CARD, r)          # strike + auto-discard
        state, ev2 = G.timeout(state, CARD, r)         # window lapses, next draws
        return state, ev + ev2

    # seat 0 times out twice…
    state, _ = timeout_turn(state, 0)
    for _ in range(3):  # seats 1..3 also drift (their strikes are their own)
        state, _ = G.timeout(state, CARD, r)
        state, _ = G.timeout(state, CARD, r)
    assert state.seats[0].strikes == 1 and state.turn == 0
    state, _ = timeout_turn(state, 0)
    for _ in range(3):
        state, _ = G.timeout(state, CARD, r)
        state, _ = G.timeout(state, CARD, r)
    assert state.seats[0].strikes == 2 and state.turn == 0
    # …then acts in time: the consecutive run breaks
    state, _ = G.discard(state, 0, state.seats[0].rack[0], CARD)
    assert state.seats[0].strikes == 0


def test_third_consecutive_timeout_goes_fallow_and_turns_skip():
    state = play_state(4, {0: "1c*14"}, wall="9d*40")
    state.seats[0].strikes = 2
    state, ev = G.timeout(state, CARD, rng())
    assert ("seat_fallow", {"seat": 0}) in ev
    assert state.seats[0].fallow
    # seat 0 auto-discarded nothing — the hand moved to seat 1's draw
    assert state.turn == 1 and state.phase is Phase.AWAIT_DISCARD
    # fallow seats are not claim responders
    state, _ = G.discard(state, 1, state.seats[1].rack[0], CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    r = rng()
    state, _ = G.claim(state, 2, "pass", [], CARD, r)
    state, _ = G.claim(state, 3, "pass", [], CARD, r)
    assert state.phase is Phase.AWAIT_DISCARD  # window closed without seat 0
    with pytest.raises(ActionRejected) as e:
        G.discard(state, 0, state.seats[0].rack[0], CARD)
    assert e.value.code is RejectCode.SEAT_FALLOW


def test_duel_fallow_ends_the_hand_with_lowest_live_line():
    state = play_state(2, {0: "1c*14", 1: "9d*13"}, wall="2b 3b")
    state.seats[1].strikes = 2
    state = open_window(state, 0, Tile("1c"))
    state, _ = G.timeout(state, CARD, rng())    # window lapses (no strike), seat 1 draws
    state, ev = G.timeout(state, CARD, rng())   # seat 1's third consecutive turn timeout
    assert ("seat_fallow", {"seat": 1}) in ev
    assert state.phase is Phase.SETTLE
    out = state.outcome
    assert out is not None and out.kind == "fallow_end" and out.winner == 0
    # a fresh-ish rack reaches a 25 line; no multipliers ever apply
    assert out.value == 25
    assert out.point_deltas == {0: 25, 1: -25}


def test_4seat_last_man_standing_collects_from_each_fallow_escrow():
    state = play_state(4, {3: "1c*14"}, turn=3, wall="9d*10")
    for s in (0, 1):
        state.seats[s].fallow = True
        state.seats[s].strikes = 3
    state.seats[2].strikes = 2
    state.turn = 2
    state.drawn = None
    state.seats[2].rack = tiles("9c*13")
    state, ev = G.timeout(state, CARD, rng())  # seat 2 folds — seat 3 stands alone
    assert state.phase is Phase.SETTLE
    out = state.outcome
    assert out is not None and out.kind == "fallow_end" and out.winner == 3
    assert out.point_deltas == {0: -out.value, 1: -out.value, 2: -out.value,
                                3: out.value * 3}


# ── Joker redemption (§2.6) ──────────────────────────────────────────────────


def redemption_state() -> GameState:
    """Seat 0 on turn; seat 1 owns a 5b pung with one joker in it."""
    return play_state(
        2,
        {0: "5b 1c*13", 1: "9d*11"},
        exposures={1: [G.ExposureState(7, Tile("5b"), 3, 1)]},
    )


def test_redemption_swaps_natural_for_joker_in_any_exposure():
    state = redemption_state()
    state, ev = G.redeem_joker(state, 0, 7, Tile("5b"))
    assert ("joker_redeemed",
            {"seat": 0, "owner": 1, "tile": "5b", "exposure_id": 7}) in ev
    assert Tile.JOKER in state.seats[0].rack
    assert Tile("5b") not in state.seats[0].rack
    exp = state.seats[1].exposures[0]
    assert exp.jokers == 0  # now all natural
    assert state.phase is Phase.AWAIT_DISCARD  # still pre-discard, same turn


def test_redemption_guards():
    state = redemption_state()
    with pytest.raises(ActionRejected) as e:  # not your turn
        G.redeem_joker(state, 1, 7, Tile("9d"))
    assert e.value.code is RejectCode.NOT_YOUR_TURN
    with pytest.raises(ActionRejected) as e:  # no such exposure
        G.redeem_joker(state, 0, 99, Tile("5b"))
    assert e.value.code is RejectCode.NO_SUCH_EXPOSURE
    with pytest.raises(ActionRejected) as e:  # tile not in rack
        G.redeem_joker(state, 0, 7, Tile("9c"))
    assert e.value.code is RejectCode.TILE_NOT_IN_RACK
    state.seats[0].rack.append(Tile("9c"))
    with pytest.raises(ActionRejected) as e:  # exposure isn't impersonating 9c
        G.redeem_joker(state, 0, 7, Tile("9c"))
    assert e.value.code is RejectCode.NOT_A_JOKER_SLOT
    state, _ = G.redeem_joker(state, 0, 7, Tile("5b"))
    with pytest.raises(ActionRejected) as e:  # no jokers left in it
        state.seats[0].rack.append(Tile("5b"))
        G.redeem_joker(state, 0, 7, Tile("5b"))
    assert e.value.code is RejectCode.NOT_A_JOKER_SLOT


def test_multiple_redemptions_one_turn():
    state = play_state(
        2,
        {0: "5b 9c 1c*12", 1: "9d*8"},
        exposures={1: [G.ExposureState(1, Tile("5b"), 3, 1),
                       G.ExposureState(2, Tile("9c"), 4, 2)]},
    )
    state, _ = G.redeem_joker(state, 0, 1, Tile("5b"))
    state, _ = G.redeem_joker(state, 0, 2, Tile("9c"))
    assert sum(1 for t in state.seats[0].rack if t is Tile.JOKER) == 2


def test_redemption_completing_the_hand_wins_as_self_pick():
    # tt-3 Old Growth: 5(F) 5(x)a 4(x)b. Seat 0 owns the 2d quint exposed
    # with one joker; redeeming their held 2d finishes F*5 + 2b*3+J.
    state = play_state(
        2,
        {0: "flower*5 2b*3 2d", 1: "9d*13"},
        exposures={0: [G.ExposureState(3, Tile("2d"), 5, 1)]},
    )
    state, _ = G.redeem_joker(state, 0, 3, Tile("2d"))
    state, ev = G.declare_mahjong_own_turn(state, 0, CARD)
    out = state.outcome
    assert out is not None and out.line_id == "tt-3" and out.won_by == "self_pick"
    assert not out.jokerless_double  # a joker sits in the winning 14


def test_invalid_self_pick_fails_privately():
    state = play_state(2, {0: "1c*14", 1: "9d*13"})
    with pytest.raises(ActionRejected) as e:
        G.declare_mahjong_own_turn(state, 0, CARD)
    assert e.value.code is RejectCode.INVALID_MAHJONG
    with pytest.raises(ActionRejected) as e:  # and never off-turn
        G.declare_mahjong_own_turn(state, 1, CARD)
    assert e.value.code is RejectCode.NOT_YOUR_TURN


# ── Scoring (§2.9) — every permutation, both modes ───────────────────────────

GH1_13 = "flower*4 2d*4 6b*4 8c"          # gh-1 (25) minus one 8c
GH1_JOKER_13 = "flower*4 2d*3 joker 6b*4 8c"  # same, one joker in a kong


def duel_win(rack13: str, via: str) -> G.Outcome:
    if via == "discard":
        state = play_state(2, {0: "8c 1c*13", 1: rack13})
        state = open_window(state, 0, Tile("8c"))
        state, _ = G.claim(state, 1, "mahjong", [], CARD, rng())
    else:
        state = play_state(2, {1: rack13 + " 8c", 0: "1c*13"}, turn=1)
        state, _ = G.declare_mahjong_own_turn(state, 1, CARD)
    assert state.outcome is not None
    return state.outcome


@pytest.mark.parametrize(
    "rack13, via, per_loser",
    [
        pytest.param(GH1_JOKER_13, "discard", 50, id="duel-discard-2x"),
        pytest.param(GH1_13, "discard", 100, id="duel-discard-jokerless-4x"),
        pytest.param(GH1_JOKER_13, "self_pick", 75, id="duel-selfpick-3x"),
        pytest.param(GH1_13, "self_pick", 150, id="duel-selfpick-jokerless-6x"),
    ],
)
def test_duel_payout_matrix(rack13, via, per_loser):
    out = duel_win(rack13, via)
    assert out.value == 25
    assert out.point_deltas == {1: per_loser, 0: -per_loser}
    assert sum(out.point_deltas.values()) == 0


def fourseat_win(rack13: str, via: str) -> G.Outcome:
    if via == "discard":
        state = play_state(4, {0: "8c 1c*13", 2: rack13})
        state = open_window(state, 0, Tile("8c"))
        r = rng()
        state, _ = G.claim(state, 1, "pass", [], CARD, r)
        state, _ = G.claim(state, 3, "pass", [], CARD, r)
        state, _ = G.claim(state, 2, "mahjong", [], CARD, r)
    else:
        state = play_state(4, {2: rack13 + " 8c"}, turn=2)
        state, _ = G.declare_mahjong_own_turn(state, 2, CARD)
    assert state.outcome is not None
    return state.outcome


@pytest.mark.parametrize(
    "rack13, via, discarder_pays, other_pays",
    [
        pytest.param(GH1_JOKER_13, "discard", 50, 25, id="4seat-discard"),
        pytest.param(GH1_13, "discard", 100, 50, id="4seat-discard-jokerless"),
        pytest.param(GH1_JOKER_13, "self_pick", 50, 50, id="4seat-selfpick"),
        pytest.param(GH1_13, "self_pick", 100, 100, id="4seat-selfpick-jokerless"),
    ],
)
def test_4seat_payout_matrix(rack13, via, discarder_pays, other_pays):
    out = fourseat_win(rack13, via)
    assert out.winner == 2
    losers = {s: -d for s, d in out.point_deltas.items() if s != 2}
    if via == "discard":
        assert losers[0] == discarder_pays
        assert losers[1] == other_pays and losers[3] == other_pays
    else:
        assert set(losers.values()) == {other_pays}
    assert out.point_deltas[2] == sum(losers.values())
    assert sum(out.point_deltas.values()) == 0


def test_quiet_pairs_gets_no_jokerless_double():
    # qp-1 (50, concealed, all pairs): jokerless by definition — no extra
    rack13 = "flower*2 1d*2 3d*2 5d*2 7d*2 9d*2 wn"
    state = play_state(2, {0: "wn 1c*13", 1: rack13})
    state = open_window(state, 0, Tile.NORTH)
    state, _ = G.claim(state, 1, "mahjong", [], CARD, rng())
    out = state.outcome
    assert out is not None and out.line_id == "qp-1"
    assert not out.jokerless_double
    assert out.point_deltas == {1: 100, 0: -100}  # 50 × 2 (discard), no double


def test_fallow_seat_still_pays_the_winner():
    state = play_state(4, {0: "8c 1c*13", 2: GH1_13})
    state.seats[3].fallow = True
    state.seats[3].strikes = 3
    state = open_window(state, 0, Tile("8c"))
    r = rng()
    state, _ = G.claim(state, 1, "pass", [], CARD, r)
    state, _ = G.claim(state, 2, "mahjong", [], CARD, r)
    out = state.outcome
    assert out is not None
    # jokerless discard win: discarder 100, others (fallow included) 50
    assert out.point_deltas[3] == -50
    assert out.point_deltas[0] == -100
    assert out.point_deltas[2] == 200


def test_wall_game_returns_zero_deltas():
    state = play_state(2, {0: "1c*14", 1: "9d*13"}, wall="")
    state = open_window(state, 0, Tile("1c"))
    state, ev = G.claim(state, 1, "pass", [], CARD, rng())
    assert any(k == "wall_game" for k, _ in ev)
    out = state.outcome
    assert out is not None and out.kind == "wall_game" and out.winner is None
    assert out.point_deltas == {0: 0, 1: 0}


# ── Rematch (§6.13, D11) ─────────────────────────────────────────────────────


def settled_state() -> GameState:
    state = play_state(2, {0: "8c 1c*13", 1: GH1_13})
    state = open_window(state, 0, Tile("8c"))
    state, _ = G.claim(state, 1, "mahjong", [], CARD, rng())
    return state


def test_rematch_needs_every_seat():
    state = settled_state()
    state, _ = G.rematch_vote(state, 0)
    assert not G.rematch_ready(state)
    with pytest.raises(ActionRejected) as e:
        G.deal(state, shuffled_wall(rng()))
    assert e.value.code is RejectCode.NOT_SETTLED
    state, ev = G.rematch_vote(state, 1)
    assert G.rematch_ready(state)
    assert ("rematch_vote", {"seat": 1, "ready": True}) in ev


def test_rematch_deal_alternates_dealer_and_resets():
    state = settled_state()
    state.seats[1].strikes = 2
    old_dealer = state.dealer
    for s in (0, 1):
        state, _ = G.rematch_vote(state, s)
    state, _ = G.deal(state, shuffled_wall(rng()))
    assert state.dealer == (old_dealer + 1) % 2
    assert state.hand_no == 2
    assert state.phase is Phase.CHARLESTON
    assert all(s.strikes == 0 and not s.fallow for s in state.seats)
    assert all(not s.exposures for s in state.seats)
    assert state.outcome is None and state.discards == []


# ── Serialization — round-trip at every phase (§10) ──────────────────────────


def state_at_phase(name: str) -> GameState:
    r = rng()
    if name == "lobby":
        state = G.create_table(TableConfig(seat_count=2), 100)
        state, _ = G.join_table(state, 101)
        return state
    state = fresh(2)
    if name == "charleston":
        state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
        return state
    if name == "charleston_blind_pending":
        for _ in range(2):
            for s in range(2):
                state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
        state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack, 1), 2, r)
        return state
    state = run_first_charleston(state)
    if name == "charleston_vote":
        state, _ = G.vote_second_charleston(state, 0, True)
        return state
    state, _ = G.vote_second_charleston(state, 0, False)
    if name == "courtesy_propose":
        state, _ = G.courtesy_propose(state, 0, 2)
        return state
    state, _ = G.courtesy_propose(state, 0, 1)
    state, _ = G.courtesy_propose(state, 1, 2)
    if name == "courtesy_pick":
        state, _ = G.courtesy_pick(state, 0, first_nonjokers(state.seats[0].rack, 1))
        return state
    state, _ = G.courtesy_pick(state, 0, first_nonjokers(state.seats[0].rack, 1))
    state, _ = G.courtesy_pick(state, 1, first_nonjokers(state.seats[1].rack, 1))
    if name == "await_discard":
        return state
    state, _ = G.discard(state, state.turn, first_nonjokers(state.seats[state.turn].rack, 1)[0], CARD)
    if name == "claim_window":
        return state
    state, _ = G.timeout(state, CARD, r)
    if name == "mid_play":
        return state
    if name == "settle":
        return settled_state()
    if name == "closed":
        state, _ = G.close_table(state, "dissolved")
        return state
    raise AssertionError(name)


@pytest.mark.parametrize("phase_name", [
    "lobby", "charleston", "charleston_blind_pending", "charleston_vote",
    "courtesy_propose", "courtesy_pick", "await_discard", "claim_window",
    "mid_play", "settle", "closed",
])
def test_serialization_round_trip(phase_name):
    state = state_at_phase(phase_name)
    d = G.state_to_dict(state)
    rebuilt = G.state_from_dict(d)
    assert G.state_to_dict(rebuilt) == d
    import json
    json.dumps(d)  # and it is actually JSON-safe


# ── Choreographed full hands (deal → Charleston → win) ───────────────────────


def test_full_duel_hand_heavenly_self_pick():
    """Dealer assembles gh-1 through a churned Charleston, votes it down,
    and declares on the first turn. Conservation holds throughout."""
    dealer = tiles("flower 2d*4 6b*4 8c*2 wn we ww")   # gh-1 minus F*3, + 3 junk
    opp = tiles("flower*3 9c*4 1b*4 ws*2")             # holds the F*3
    state = fresh(2, racks=[dealer, opp])
    r = rng()
    J = tiles("wn we ww")
    F3 = tiles("flower*3")
    K = tiles("9c*3")
    # P1: dealer sends junk; opp sends its own junk back
    state, _ = G.charleston_pick(state, 0, J, 0, r)
    state, _ = G.charleston_pick(state, 1, K, 0, r)
    assert total_tiles(state) == 152
    # P2: both return what they just received
    state, _ = G.charleston_pick(state, 0, K, 0, r)
    state, _ = G.charleston_pick(state, 1, J, 0, r)
    # P3: dealer sheds its last junk; opp hands over the flowers
    state, _ = G.charleston_pick(state, 0, J, 0, r)
    state, _ = G.charleston_pick(state, 1, F3, 0, r)
    assert state.phase is Phase.CHARLESTON_VOTE
    state, _ = G.vote_second_charleston(state, 0, False)
    state, _ = G.courtesy_propose(state, 0, 0)
    state, _ = G.courtesy_propose(state, 1, 0)
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 0
    assert total_tiles(state) == 152
    state, ev = G.declare_mahjong_own_turn(state, 0, CARD)
    out = state.outcome
    assert out is not None and out.line_id == "gh-1" and out.won_by == "self_pick"
    assert out.jokerless_double
    assert out.point_deltas == {0: 150, 1: -150}  # 25 × 3 × 2


def test_full_4seat_hand_choreographed_self_pick():
    """Three seats each ferry the dealer a missing trio on the pass that
    routes to seat 0 — right, across, left in turn."""
    dealer = tiles("flower 2d 6b 8c*2 1c*3 3c*3 5c*3")  # 5 keepers + 9 junk
    ferry1 = tiles("flower*3 9c*4 wn*3 we*3")          # sends F*3 on P3 (left)
    ferry2 = tiles("6b*3 9d*4 ww*3 ws*3")              # sends 6b*3 on P2 (across)
    ferry3 = tiles("2d*3 9b*4 dr*3 dg*3")              # sends 2d*3 on P1 (right)
    state = fresh(4, racks=[dealer, ferry1, ferry2, ferry3])
    r = rng()

    def junk(seat, n=3):
        keep = {0: tiles("flower 2d*4 6b*4 8c*2"),
                1: tiles("flower*3"), 2: tiles("6b*3"), 3: tiles("2d*3")}[seat]
        keepc = Counter(keep)
        rack = Counter(state.seats[seat].rack)
        pool: list[Tile] = []
        for t, cnt in (rack - keepc).items():
            if t is not Tile.JOKER:
                pool.extend([t] * cnt)
        return pool[:n]

    # P1 (right): seat 3's trio reaches seat 0
    state, _ = G.charleston_pick(state, 0, junk(0), 0, r)
    state, _ = G.charleston_pick(state, 1, junk(1), 0, r)
    state, _ = G.charleston_pick(state, 2, junk(2), 0, r)
    state, _ = G.charleston_pick(state, 3, tiles("2d*3"), 0, r)
    # P2 (across): seat 2's trio reaches seat 0
    state, _ = G.charleston_pick(state, 0, junk(0), 0, r)
    state, _ = G.charleston_pick(state, 1, junk(1), 0, r)
    state, _ = G.charleston_pick(state, 2, tiles("6b*3"), 0, r)
    state, _ = G.charleston_pick(state, 3, junk(3), 0, r)
    # P3 (left): seat 1's trio reaches seat 0
    state, _ = G.charleston_pick(state, 0, junk(0), 0, r)
    state, _ = G.charleston_pick(state, 1, tiles("flower*3"), 0, r)
    state, _ = G.charleston_pick(state, 2, junk(2), 0, r)
    state, _ = G.charleston_pick(state, 3, junk(3), 0, r)
    assert state.phase is Phase.CHARLESTON_VOTE
    state, _ = G.vote_second_charleston(state, 2, False)
    for s in range(4):
        state, _ = G.courtesy_propose(state, s, 0)
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 0
    assert total_tiles(state) == 152
    state, _ = G.declare_mahjong_own_turn(state, 0, CARD)
    out = state.outcome
    assert out is not None and out.line_id == "gh-1"
    # 4-seat self-pick, jokerless: everyone pays 25 × 2 × 2 = 100
    assert out.point_deltas == {1: -100, 2: -100, 3: -100, 0: 300}


def test_call_then_declare_scores_as_discard_win_not_self_pick():
    """D15 (stage-3 review): calling your winning tile then declaring must
    not upgrade a discard win into a self-pick payout."""
    # seat 1 goes gh-1; the 6b kong via a call completes their 14
    state = play_state(2, {0: "6b 1c*13", 1: "flower*4 2d*4 6b*3 8c*2"})
    state = open_window(state, 0, Tile("6b"))
    state, _ = G.claim(state, 1, "call", tiles("6b*3"), CARD, rng())
    assert state.turn == 1 and state.turn_source == "call"
    state, _ = G.declare_mahjong_own_turn(state, 1, CARD)
    out = state.outcome
    assert out is not None and out.won_by == "discard" and out.discarder == 0
    # Duel discard win, jokerless: 25 × 2 × 2 = 100 — NOT the 150 self-pick
    assert out.point_deltas == {1: 100, 0: -100}


def test_declare_after_a_real_draw_is_self_pick_again():
    state = play_state(2, {0: "1c*14", 1: "flower*4 2d*4 6b*4 8c"}, wall="8c")
    state = open_window(state, 0, Tile("1c"))
    state, _ = G.claim(state, 1, "pass", [], CARD, rng())  # draws the 8c
    assert state.turn_source == "draw"
    state, _ = G.declare_mahjong_own_turn(state, 1, CARD)
    out = state.outcome
    assert out is not None and out.won_by == "self_pick" and out.discarder is None


def test_oversized_wall_trim_caps_instead_of_crashing():
    state = G.create_table(TableConfig(seat_count=2, wall_trim=9999), 100)
    state, _ = G.join_table(state, 101)
    state, _ = G.deal(state, shuffled_wall(rng()))
    assert state.phase is Phase.CHARLESTON
    assert len(state.wall) == 0  # everything beyond the deal was trimmed
    assert len(state.seats[0].rack) == 14 and len(state.seats[1].rack) == 13


def test_call_that_would_empty_the_rack_downgrades_privately():
    """Stage-3 review: exposing your whole rack leaves nothing to discard —
    a wedged seat and a crashing timer. Refused as a private pass."""
    exposures = {1: [G.ExposureState(1, Tile("9d"), 4, 0),
                     G.ExposureState(2, Tile("9b"), 4, 0),
                     G.ExposureState(3, Tile("9c"), 3, 0)]}
    state = play_state(2, {0: "5b 1c*13", 1: "5b*2"}, exposures=exposures, wall="2b")
    state = open_window(state, 0, Tile("5b"))
    state, ev = G.claim(state, 1, "call", tiles("5b*2"), CARD, rng())
    assert ("claim_downgraded", {"seat": 1, "why": "no_discard_left", "private": True}) in ev
    # one tile spare and the same call stands
    state = play_state(2, {0: "5b 1c*13", 1: "5b*2 9d"},
                       exposures={1: [G.ExposureState(1, Tile("9c"), 4, 0),
                                      G.ExposureState(2, Tile("9b"), 4, 0)]},
                       wall="2b")
    state = open_window(state, 0, Tile("5b"))
    state, ev = G.claim(state, 1, "call", tiles("5b*2"), CARD, rng())
    assert any(k == "exposure_made" for k, _ in ev)


@pytest.mark.parametrize("seat_count", [2, 4])
def test_simultaneous_all_afk_fold_refunds_instead_of_paying_an_index(seat_count):
    """Stage-3 review: when every seat times out to its third strike in one
    batch, nobody 'survives' by seat order — the hand voids, escrow returns."""
    state = fresh(seat_count)
    for s in state.seats:
        s.strikes = 2
    state, ev = G.timeout(state, CARD, rng())  # nobody submitted a pick
    assert state.phase is Phase.SETTLE
    out = state.outcome
    assert out is not None and out.kind == "all_fallow" and out.winner is None
    assert set(out.point_deltas.values()) == {0}


def test_partial_batch_fold_still_pays_the_present_seat():
    # three seats at two strikes; seat 0 alone submitted its pick — the one
    # member actually present collects the fallow end
    state = fresh(4)
    for s in (1, 2, 3):
        state.seats[s].strikes = 2
    r = rng()
    state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
    state, ev = G.timeout(state, CARD, r)
    assert state.phase is Phase.SETTLE
    out = state.outcome
    assert out is not None and out.kind == "fallow_end" and out.winner == 0


def test_claim_pass_does_not_shield_a_turn_afk_player():
    """D17: a free window Pass must not reset the consecutive-timeout run."""
    state = play_state(2, {0: "1c*14", 1: "9d*13"}, wall="9b*20")
    state.seats[1].strikes = 2
    state = open_window(state, 0, Tile("1c"))
    state, _ = G.claim(state, 1, "pass", [], CARD, rng())  # tap, not play
    assert state.seats[1].strikes == 2  # unchanged
    state, ev = G.timeout(state, CARD, rng())  # their turn times out — third
    assert ("seat_fallow", {"seat": 1}) in ev


def test_redeeming_from_a_fallow_seats_exposure_is_allowed():
    """D16: exposed tiles are on the table even when the hand died."""
    state = play_state(
        2, {0: "5b 1c*13", 1: "9d*11"},
        exposures={1: [G.ExposureState(7, Tile("5b"), 3, 1)]},
    )
    state.seats[1].fallow = True
    state.seats[1].strikes = 3
    state, ev = G.redeem_joker(state, 0, 7, Tile("5b"))
    assert any(k == "joker_redeemed" for k, _ in ev)


def test_every_claim_downgrade_is_marked_private():
    """§2.5's 'a tap never leaks': the glue layer keys off data['private']."""
    cases = [
        # illegal call (pair-call)
        (play_state(2, {0: "5b 1c*13", 1: "5b 9d*12"}), "call", tiles("5b")),
        # invalid mahjong
        (play_state(4, {0: "5b 1c*13", 1: "9d*13"}), "mahjong", []),
    ]
    for base, kind, claim_tiles in cases:
        state = open_window(base, 0, Tile("5b"))
        _, ev = G.claim(state, 1, kind, claim_tiles, CARD, rng())
        downgrades = [d for k, d in ev if k == "claim_downgraded"]
        assert downgrades and all(d.get("private") is True for d in downgrades)


# ── Coverage sweep (stage-3 review, coverage lens) ───────────────────────────


def test_table_config_validation():
    with pytest.raises(ValueError, match="seat_count"):
        TableConfig(seat_count=3)
    with pytest.raises(ValueError, match="wall_trim"):
        TableConfig(seat_count=2, wall_trim=-1)


def test_unseated_and_out_of_range_actors_reject():
    state = play_state(2, {0: "1c*14", 1: "9d*13"})
    with pytest.raises(ActionRejected) as e:
        G.discard(state, 7, Tile("1c"), CARD)
    assert e.value.code is RejectCode.NOT_SEATED
    settled = settled_state()
    with pytest.raises(ActionRejected) as e:
        G.rematch_vote(settled, 7)
    assert e.value.code is RejectCode.NOT_SEATED


def test_blind_count_bounds():
    state = fresh(2)
    r = rng()
    for _ in range(2):
        for s in range(2):
            state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
    with pytest.raises(ActionRejected) as e:
        G.charleston_pick(state, 0, [], 4, r)
    assert e.value.code is RejectCode.BAD_COUNT
    with pytest.raises(ActionRejected) as e:
        G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), -1, r)
    assert e.value.code is RejectCode.BAD_COUNT


def test_double_vote_and_double_claim_and_bad_kind():
    state = run_first_charleston(fresh(2))
    state, _ = G.vote_second_charleston(state, 0, True)
    with pytest.raises(ActionRejected) as e:
        G.vote_second_charleston(state, 0, True)
    assert e.value.code is RejectCode.ALREADY_SUBMITTED

    play = play_state(4, {0: "1c*14"}, filler=CLAIMABLE_FILLER)
    play = open_window(play, 0, Tile("1c"))
    play, _ = G.claim(play, 1, "pass", [], CARD, rng())
    with pytest.raises(ActionRejected) as e:
        G.claim(play, 1, "pass", [], CARD, rng())
    assert e.value.code is RejectCode.ALREADY_SUBMITTED
    with pytest.raises(ActionRejected) as e:
        G.claim(play, 2, "chicken", [], CARD, rng())
    assert e.value.code is RejectCode.BAD_COUNT


def test_courtesy_pick_guards_nothing_owed_and_double_and_shortfall():
    state = to_courtesy(fresh(4))
    for s, n in ((0, 2), (1, 0), (2, 2), (3, 0)):
        state, _ = G.courtesy_propose(state, s, n)
    assert state.courtesy_owed == {0: 2, 2: 2}
    with pytest.raises(ActionRejected) as e:  # seat 1 owes nothing
        G.courtesy_pick(state, 1, first_nonjokers(state.seats[1].rack, 2))
    assert e.value.code is RejectCode.BAD_COUNT
    missing = next(t for t in Tile if t not in state.seats[0].rack and t is not Tile.JOKER)
    with pytest.raises(ActionRejected) as e:  # tiles not held
        G.courtesy_pick(state, 0, [missing, missing])
    assert e.value.code is RejectCode.TILE_NOT_IN_RACK
    state, _ = G.courtesy_pick(state, 0, first_nonjokers(state.seats[0].rack, 2))
    with pytest.raises(ActionRejected) as e:  # double submit
        G.courtesy_pick(state, 0, first_nonjokers(state.seats[0].rack, 2))
    assert e.value.code is RejectCode.ALREADY_SUBMITTED


def test_engine_timeout_rejects_in_lobby_and_settle():
    lobby = G.create_table(TableConfig(seat_count=2), 100)
    with pytest.raises(ActionRejected) as e:
        G.timeout(lobby, CARD, rng())
    assert e.value.code is RejectCode.WRONG_PHASE
    with pytest.raises(ActionRejected):
        G.timeout(settled_state(), CARD, rng())


def test_full_second_charleston_runs_left_across_right_to_courtesy():
    """The whole optional Charleston at both seat counts, ending in courtesy."""
    for seat_count in (2, 4):
        state = run_first_charleston(fresh(seat_count))
        for s in range(seat_count):
            state, _ = G.vote_second_charleston(state, s, True)
        r = rng()
        offsets = (3, 2, 1) if seat_count == 4 else (1, 1, 1)
        for expected_offset in offsets:
            markers = {s: first_nonjokers(state.seats[s].rack)
                       for s in range(seat_count)}
            for s in range(seat_count):
                state, _ = G.charleston_pick(state, s, markers[s], 0, r)
            for s in range(seat_count):
                rack = Counter(state.seats[(s + expected_offset) % seat_count].rack)
                assert all(rack[t] >= n for t, n in Counter(markers[s]).items())
        assert state.phase is Phase.COURTESY_PROPOSE
        assert total_tiles(state) == 152


def test_partial_blind_cycle_break_conserves_and_duplicates_nothing():
    """D12's other branch: a mutual-blind cycle where both seats also picked
    — the pool dedup must not clone the picked copies."""
    state = fresh(2)
    r = rng()
    for _ in range(2):
        for s in range(2):
            state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
    pick0 = first_nonjokers(state.seats[0].rack, 1)
    pick1 = first_nonjokers(state.seats[1].rack, 2)
    state, _ = G.charleston_pick(state, 0, pick0, 2, r)
    state, ev = G.charleston_pick(state, 1, pick1, 1, r)
    assert any(k == "pass_resolved" for k, _ in ev)
    assert total_tiles(state) == 152
    # physical-copy audit: no tile kind exceeds its real count anywhere
    everything = Counter()
    for s in state.seats:
        everything.update(s.rack)
    everything.update(state.wall)
    from bot_modules.games.mahjong.tiles import copies
    assert all(n <= copies(t) for t, n in everything.items())


@pytest.mark.parametrize("phase_name", ["vote", "propose", "pick"])
def test_all_afk_fold_settles_from_every_simultaneous_phase(phase_name):
    state = fresh(2)
    r = rng()
    for _ in range(3):
        for s in range(2):
            state, _ = G.charleston_pick(state, s, first_nonjokers(state.seats[s].rack), 0, r)
    assert state.phase is Phase.CHARLESTON_VOTE
    if phase_name != "vote":
        state, _ = G.vote_second_charleston(state, 0, False)
        if phase_name == "pick":
            state, _ = G.courtesy_propose(state, 0, 1)
            state, _ = G.courtesy_propose(state, 1, 1)
            assert state.phase is Phase.COURTESY_PICK
    for s in state.seats:
        s.strikes = 2
    state, ev = G.timeout(state, CARD, r)
    out = state.outcome
    assert out is not None and out.kind == "all_fallow"


def test_4seat_wall_game_and_rematch_rotation():
    # wall game with four seats: all zero deltas
    state = play_state(4, {0: "1c*14"}, wall="", filler=CLAIMABLE_FILLER)
    state = open_window(state, 0, Tile("1c"))
    r = rng()
    for s in (1, 2):
        state, _ = G.claim(state, s, "pass", [], CARD, r)
    state, _ = G.claim(state, 3, "pass", [], CARD, r)
    out = state.outcome
    assert out is not None and out.kind == "wall_game"
    assert out.point_deltas == {0: 0, 1: 0, 2: 0, 3: 0}
    # §2.2: the dealer rotates one seat per hand on rematch
    for s in range(4):
        state, _ = G.rematch_vote(state, s)
    old_dealer = state.dealer
    state, _ = G.deal(state, shuffled_wall(r))
    assert state.dealer == (old_dealer + 1) % 4


def test_serialization_round_trips_a_rich_state():
    """Exposures with jokers, recorded claims, strikes, fallow, and a
    non-mahjong outcome all survive the codec byte-identically."""
    state = play_state(
        2, {0: "5b 1c*12", 1: "9d*10"},
        exposures={1: [G.ExposureState(3, Tile("6b"), 4, 2)]},
    )
    state.seats[0].strikes = 2
    state.seats[1].fallow = True
    state.claims = {1: ("call", [Tile("5b"), Tile.JOKER])}
    state.turn_source = "call"
    state.claimed_discarder = 1
    state.outcome = G.Outcome(
        kind="all_fallow", winner=None, line_id=None, line_name=None,
        value=0, won_by=None, jokerless_double=False, discarder=None,
        point_deltas={0: 0, 1: 0},
    )
    d = G.state_to_dict(state)
    rebuilt = G.state_from_dict(d)
    assert G.state_to_dict(rebuilt) == d
    assert rebuilt.seats[1].exposures[0].jokers == 2
    assert rebuilt.claims[1] == ("call", [Tile("5b"), Tile.JOKER])


def test_fallow_end_counts_the_folded_seats_exposures_as_seen():
    """The folded seat's exposure kills lines for the survivor: its three
    natural reds plus one in the pit exhaust qp-3's 2(R) pair."""
    survivor_rack = "1d*2 2d*2 3d*2 1b*2 2b*2 3b*2 9c"  # qp-3 minus the dr pair
    base = play_state(
        2, {0: survivor_rack, 1: "9d*10"},
        exposures={1: [G.ExposureState(1, Tile("dr"), 4, 1)]},  # 3 naturals + J
    )
    base.discards = [(1, Tile("dr"))]  # the fourth red died in the pit
    base.seats[1].strikes = 3
    base.seats[1].fallow = True
    from bot_modules.games.mahjong.game_logic import _settle_fallow_end
    state = base  # helper mutates; fine in a test
    _settle_fallow_end(state, CARD)
    out = state.outcome
    assert out is not None and out.kind == "fallow_end"
    # qp-3 (75) is DEAD — all four reds are seen; the payout used a live line
    live_values = [h.value for h in CARD.hands if h.id != "qp-3"]
    assert out.value in live_values or out.value == 25


def test_charleston_autopass_never_moves_jokers():
    state = fresh(2)
    # plant jokers in seat 1's rack, then let the phase time out
    state.seats[1].rack[:3] = [Tile.JOKER] * 3
    jokers_before = sum(1 for t in state.seats[0].rack if t is Tile.JOKER)
    r = rng()
    state, _ = G.charleston_pick(state, 0, first_nonjokers(state.seats[0].rack), 0, r)
    state, _ = G.timeout(state, CARD, r)
    jokers_after = sum(1 for t in state.seats[0].rack if t is Tile.JOKER)
    assert jokers_after == jokers_before  # seat 1's auto-pass kept its jokers


def test_full_turn_loop_hand_call_redemption_discard_win():
    """The whole §2.4–§2.7 loop in one scripted Duel: Charleston shuttle,
    a claim-call with a joker in it, the dealer redeeming that joker on a
    later turn, and the win claimed off a discard — jokerless, because the
    redemption pulled the joker OUT of the winning 14."""
    dealer = tiles("6b*2 8c flower*3 9b*4 9c*4")        # feeds + shuttle fuel
    opp = tiles("flower 2d*4 6b*2 joker 8c 9d*4")       # gh-1 shell + junk
    state = fresh(2, racks=[dealer, opp], tail=[Tile("1b")],
                  second_charleston=False)
    r = rng()
    F3, D3, N3 = tiles("flower*3"), tiles("9d*3"), tiles("2d*3")

    # Charleston shuttle: flowers ride to the opponent, junk rides back
    state, _ = G.charleston_pick(state, 0, F3, 0, r)    # P1: F3 →
    state, _ = G.charleston_pick(state, 1, D3, 0, r)    #     ← 9d3
    state, _ = G.charleston_pick(state, 0, D3, 0, r)    # P2: 9d3 back →
    state, _ = G.charleston_pick(state, 1, N3, 0, r)    #     ← 2d3
    state, _ = G.charleston_pick(state, 0, N3, 0, r)    # P3: 2d3 back →
    state, _ = G.charleston_pick(state, 1, D3, 0, r)    #     ← 9d3
    assert state.phase is Phase.COURTESY_PROPOSE        # lever off skips vote
    state, _ = G.courtesy_propose(state, 0, 0)
    state, _ = G.courtesy_propose(state, 1, 0)
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 0
    assert total_tiles(state) == 152
    assert Counter(state.seats[1].rack) == Counter(
        tiles("flower*4 2d*4 6b*2 joker 8c 9d"))

    # Turn 1: dealer feeds the 6b; opp calls a kong with its joker riding
    state, _ = G.discard(state, 0, Tile("6b"), CARD)
    state, ev = G.claim(state, 1, "call", tiles("6b*2 joker"), CARD, r)
    assert any(k == "exposure_made" for k, _ in ev)
    exp = state.seats[1].exposures[0]
    assert (exp.natural, exp.count, exp.jokers) == (Tile("6b"), 4, 1)
    assert state.turn == 1 and state.turn_source == "call"
    state, _ = G.discard(state, 1, Tile("9d"), CARD)          # the claimant's discard

    # window lapses; the dealer draws the scripted junk tile
    state, _ = G.claim(state, 0, "pass", [], CARD, r)
    assert state.turn == 0 and state.drawn is Tile("1b")

    # Turn 2: dealer redeems the joker out of the opponent's kong (§2.6 —
    # any exposure), then feeds the winning 8c
    state, ev = G.redeem_joker(state, 0, exp.exposure_id, Tile("6b"))
    assert any(k == "joker_redeemed" for k, _ in ev)
    assert Tile.JOKER in state.seats[0].rack
    assert state.seats[1].exposures[0].jokers == 0
    state, _ = G.discard(state, 0, Tile("8c"), CARD)
    state, ev = G.claim(state, 1, "mahjong", [], CARD, r)

    out = state.outcome
    assert out is not None and out.kind == "mahjong"
    assert out.line_id == "gh-1" and out.won_by == "discard" and out.discarder == 0
    assert out.jokerless_double  # the redemption took the joker out of the 14
    assert out.point_deltas == {1: 100, 0: -100}  # 25 × 2 × 2
    assert total_tiles(state) == 152


def test_force_fallow_on_turn_advances_the_hand():
    """A guild leaver on turn: the hand moves straight on (stage-6 review)."""
    state = play_state(4, {0: "1c*14"}, wall="9d*5")
    state, ev = G.force_fallow(state, 0, CARD, rng())
    assert ("seat_fallow", {"seat": 0, "left_guild": True}) in ev
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 1
    assert len(state.seats[1].rack) == 14  # drew without waiting for a timer


def test_force_fallow_last_responder_resolves_the_window():
    state = play_state(4, {0: "1c*14"}, wall="9d*5", filler=CLAIMABLE_FILLER)
    state = open_window(state, 0, Tile("1c"))
    r = rng()
    state, _ = G.claim(state, 1, "pass", [], CARD, r)
    state, _ = G.claim(state, 2, "pass", [], CARD, r)
    # seat 3 leaves the guild — the window stops waiting for them
    state, ev = G.force_fallow(state, 3, CARD, r)
    assert state.phase is Phase.AWAIT_DISCARD and state.turn == 1


def test_force_fallow_duel_settles_immediately():
    state = play_state(2, {0: "1c*14", 1: "9d*13"})
    state, ev = G.force_fallow(state, 1, CARD, rng())
    out = state.outcome
    assert out is not None and out.kind == "fallow_end" and out.winner == 0


# ── Assistance readout (plans/mahjong-assist.md stage 3) ─────────────────────


def test_assist_readout_gates():
    state = play_state(2, {0: "flower*4 2d*4 6b*4 8c", 1: "9c*13"})
    # a live mode in a decision phase renders
    r = G.assist_readout(state, 0, CARD, "target")
    assert r is not None and r.mode == "target"
    # off renders nothing, as does an unknown mode
    assert G.assist_readout(state, 0, CARD, "off") is None
    assert G.assist_readout(state, 0, CARD, "banana") is None
    # a fallow seat has no decisions left
    state.seats[0].fallow = True
    assert G.assist_readout(state, 0, CARD, "target") is None
    state.seats[0].fallow = False
    # settle/lobby phases have no tile choice in them
    state.phase = Phase.SETTLE
    assert G.assist_readout(state, 0, CARD, "target") is None
    state.phase = Phase.LOBBY
    state.seats[0].rack = []
    assert G.assist_readout(state, 0, CARD, "target") is None


def test_assist_readout_caps_at_three_but_counts_all():
    state = play_state(2, {0: "flower*2 1d*2 9d 2b*3 wn*2 dr 5c*2", 1: "9c*13"})
    r = G.assist_readout(state, 0, CARD, "gap")
    assert r is not None
    assert len(r.prospects) <= 3
    assert r.live_count >= len(r.prospects)
    # ranked: distances never decrease across the shown prospects
    ds = [p.distance for p in r.prospects]
    assert ds == sorted(ds)


def test_assist_suggestion_only_in_coach():
    racks = {0: "flower*4 2d*4 6b*2 9d wn", 1: "9c*13"}
    r_gap = G.assist_readout(play_state(2, racks), 0, CARD, "gap")
    r_coach = G.assist_readout(play_state(2, racks), 0, CARD, "coach")
    assert r_gap is not None and r_gap.suggestion is None
    assert r_coach is not None and r_coach.suggestion is not None


def test_assist_coach_respects_the_visible_threat_rail():
    # Seat 1's exposed 9d kong makes the nines hot (gh-3 wants the sisters);
    # seat 0's only dead weight is 9b — coach must go silent, not feed it.
    exposures = {1: [G.ExposureState(exposure_id=1, natural=Tile("9d"),
                                     count=4, jokers=0)]}
    state = play_state(
        2, {0: "flower*4 2d*4 6b*2 9b*3", 1: "9c*9"}, exposures=exposures
    )
    r = G.assist_readout(state, 0, CARD, "coach")
    assert r is not None
    assert dict(r.prospects[0].dead_weight) == {Tile("9b"): 3}
    assert r.suggestion is None
    # take the exposure away and the same rack gets its suggestion back
    state2 = play_state(2, {0: "flower*4 2d*4 6b*2 9b*3", 1: "9c*13"})
    r2 = G.assist_readout(state2, 0, CARD, "coach")
    assert r2 is not None and r2.suggestion == Tile("9b")


def test_assist_uses_discards_and_exposures_as_seen():
    # The same rack loses a line's cheapest binding when the discard pile
    # holds the last copies — seen_elsewhere is really wired through.
    state = play_state(2, {0: "flower*2 1d*3 3d*3 5d*3 7d", 1: "9c*13"})
    base = G.assist_readout(state, 0, CARD, "gap")
    state.discards = [(1, Tile("7d"))] * 3 + [(1, Tile.JOKER)] * 8
    after = G.assist_readout(state, 0, CARD, "gap")
    assert base is not None and after is not None
    sb1_base = next(p for p in base.prospects if p.hand.id == "sb-1")
    sb1_after = next((p for p in after.prospects if p.hand.id == "sb-1"), None)
    assert sb1_base.distance == 2
    assert sb1_after is None or sb1_after.distance > 2


def test_seen_elsewhere_matches_the_fallow_settle_view():
    # The extraction refactor: the helper and the fallow settle must agree.
    exposures = {1: [G.ExposureState(exposure_id=1, natural=Tile("2b"),
                                     count=3, jokers=1)]}
    state = play_state(2, {0: "flower*4 2d*4 6b*4 8c", 1: "9c*10"},
                       exposures=exposures)
    state.discards = [(0, Tile("5c")), (1, Tile("5c")), (0, Tile("wn"))]
    seen = G.seen_elsewhere(state, 0)
    assert seen[Tile("5c")] == 2
    assert seen[Tile("wn")] == 1
    assert seen[Tile("2b")] == 2      # naturals of the exposure
    assert seen[Tile.JOKER] == 1      # its joker counts as a joker
    assert Tile("2d") not in seen     # own tiles are never "elsewhere"


def test_claim_window_live_discard_counts_as_obtainable_for_claimants():
    # Review finding (stage 4): the live discard is the one tile the claim
    # window exists to acquire. Both seats sit one 8c short of Golden Hour;
    # two 8c lie dead in the pit; seat 1 discards the third... fourth copy.
    state = play_state(
        2, {0: "flower*4 2d*4 6b*4 8c", 1: "flower*4 2d*4 6b*4 8c"}, turn=1,
    )
    state.discards = [(0, Tile("8c")), (1, Tile("8c"))]
    state, _ = G.discard(state, 1, Tile("8c"), CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    assert state.live_discard is Tile("8c")
    # The claimant is told the truth: one tile away, and it's the live one.
    r0 = G.assist_readout(state, 0, CARD, "gap")
    assert r0 is not None
    gh1 = next(p for p in r0.prospects if p.hand.id == "gh-1")
    assert gh1.distance == 1
    assert dict(gh1.needed) == {Tile("8c"): 1}
    # The discarder can never reclaim their own tile — for them it IS gone,
    # and gh-1 only survives via a sister-suit binding at a real distance.
    r1 = G.assist_readout(state, 1, CARD, "gap")
    assert r1 is not None
    gh1_d = next((p for p in r1.prospects if p.hand.id == "gh-1"), None)
    assert gh1_d is None or gh1_d.distance > 1


# ── Review round 2 (code-review 2026-08-22): fallow settle vs live discard ───

FALLOW_VALUE_CARD = load_card({
    "card_id": "t-fallow-value", "display_name": "Fallow Value", "season": "t",
    "hands": [
        {"id": "cheap", "section": "T", "name": "Cheap", "concealed": False,
         "value": 25,
         "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "1", "suit": "a"},
                    {"count": 4, "rank": "6", "suit": "b"}, {"count": 2, "rank": "8", "suit": "c"}]},
        {"id": "rich", "section": "T", "name": "Rich", "concealed": False,
         "value": 50,
         "groups": [{"count": 4, "rank": "F"}, {"count": 4, "rank": "N"},
                    {"count": 4, "rank": "S"}, {"count": 2, "rank": "R"}]},
    ],
})


def test_fallow_settle_counts_the_live_discard_as_takeable():
    # A leaver folds during the claim window. The survivor sits one 8c from
    # the cheap 25-pointer, every sister-suit 8 is dead in the pit, and the
    # live discard IS the missing 8c — a tile the survivor could have
    # claimed. Valuing their hand as if that tile were extinct calls the
    # cheap line dead and pays the survivor the rich line's 50 per seat:
    # a real-coin overpayment. Amendment 2's "still reachable" must see the
    # claimable tile (the same view the readout was fixed to in R1).
    state = play_state(
        2, {0: "flower*4 1d*4 6b*4 8c", 1: "9c*12 8c"}, turn=1,
    )
    state.discards = (
        [(0, Tile("8d"))] * 4 + [(0, Tile("8b"))] * 4 + [(1, Tile("8c"))] * 2
    )
    state, _ = G.discard(state, 1, Tile("8c"), FALLOW_VALUE_CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    state, ev = G.force_fallow(state, 1, FALLOW_VALUE_CARD, rng())
    assert state.outcome is not None and state.outcome.kind == "fallow_end"
    assert state.outcome.value == 25  # cheap line live via the claimable 8c


def test_fallow_settle_discarders_own_tile_stays_gone():
    # Same shape, but the SURVIVOR is the one who discarded the 8c — their
    # own tile can never come back to them, so the cheap line is truly dead
    # and the valuation correctly lands on the rich line.
    state = play_state(
        2, {0: "flower*4 1d*4 6b*4 8c*2", 1: "9c*13"}, turn=0,
    )
    state.discards = (
        [(0, Tile("8d"))] * 4 + [(0, Tile("8b"))] * 4 + [(1, Tile("8c"))] * 2
    )
    state, _ = G.discard(state, 0, Tile("8c"), CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    state, ev = G.force_fallow(state, 1, FALLOW_VALUE_CARD, rng())
    assert state.outcome is not None and state.outcome.kind == "fallow_end"
    assert state.outcome.value == 50


def test_claim_window_discount_stops_after_a_pass():
    # F4: a seat that already passed has renounced the tile — its readout
    # must not keep calling the live discard takeable. 4-seat, so two other
    # claimants keep the window open after seat 0's pass.
    state = play_state(
        4, {0: "flower*4 2d*4 6b*4 8c", 1: "9c*12 8c"}, turn=1,
        filler=CLAIMABLE_FILLER,
    )
    state.discards = [(2, Tile("8c")), (3, Tile("8c"))]
    state, _ = G.discard(state, 1, Tile("8c"), CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    before = G.assist_readout(state, 0, CARD, "gap")
    gh1 = next(p for p in before.prospects if p.hand.id == "gh-1")
    assert gh1.distance == 1  # takeable while unresponded
    state, _ = G.claim(state, 0, "pass", [], CARD, rng())
    assert state.phase is Phase.CLAIM_WINDOW  # seats 2 and 3 still out
    after = G.assist_readout(state, 0, CARD, "gap")
    gh1_after = next((p for p in after.prospects if p.hand.id == "gh-1"), None)
    assert gh1_after is None or gh1_after.distance > 1


def test_fallow_seats_exposures_never_gate_the_coach():
    # F3: a folded seat can never claim or win — its exposed kong reveals
    # nothing a live seat threatens, so it must not silence a suggestion.
    exposures = {1: [G.ExposureState(exposure_id=1, natural=Tile("9d"),
                                     count=4, jokers=0)]}
    state = play_state(
        2, {0: "flower*4 2d*4 6b*2 9b*3", 1: "9c*9"}, exposures=exposures
    )
    silenced = G.assist_readout(state, 0, CARD, "coach")
    assert silenced is not None and silenced.suggestion is None  # live: rail
    state.seats[1].fallow = True
    freed = G.assist_readout(state, 0, CARD, "coach")
    assert freed is not None and freed.suggestion == Tile("9b")


@pytest.mark.parametrize("mode", ASSIST_MODES)
def test_every_assist_mode_is_understood_by_the_gate(mode):
    # F7: the vocabulary must have ONE authority. Every mode the service
    # accepts either renders a readout or is 'off' — a fifth mode silently
    # rendering nothing (indistinguishable from off) is the failure pinned.
    state = play_state(2, {0: "flower*4 2d*4 6b*4 8c", 1: "9c*13"})
    r = G.assist_readout(state, 0, CARD, mode)
    assert (r is None) == (mode == "off")


# ── Verify round on the round-2 fixes (V1): a legal route, or no discount ────


def test_fallow_settle_pair_mate_two_away_is_not_takeable():
    # V1a: a pair tile can only arrive as an instant Mahjong (pairs can't be
    # called). Two tiles out, the claim wouldn't complete the hand and the
    # seat holds only one matching tile — no legal route, so the cheap line
    # is truly dead and the discount must not resurrect it (which would
    # UNDERPAY the survivor: fallow pays the minimum live line).
    state = play_state(
        2, {0: "flower*4 1d*4 6b*3 8c 9b", 1: "9c*12 8c"}, turn=1,
    )
    state.discards = (
        [(0, Tile("8d"))] * 4 + [(0, Tile("8b"))] * 4 + [(1, Tile("8c"))] * 2
    )
    state, _ = G.discard(state, 1, Tile("8c"), CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    state, _ = G.force_fallow(state, 1, FALLOW_VALUE_CARD, rng())
    assert state.outcome is not None
    assert state.outcome.value == 50  # cheap stays dead: no route to the 8c


def test_fallow_settle_discarded_joker_is_unclaimable():
    # V1b: a discarded joker can never be claimed by anyone (§2.5). It must
    # never be discounted — doing so loosens the joker-deficit liveness test
    # for every line at once.
    state = play_state(
        2, {0: "flower*4 1d*4 6b*3 8c*2", 1: "9c*12 joker"}, turn=1,
    )
    # every sister-suit binding must die too, or reachability just re-binds
    # the line to an open suit and the joker question never arises
    state.discards = (
        [(0, Tile("6b"))] + [(0, Tile.JOKER)] * 7
        + [(0, Tile("6d"))] * 4 + [(0, Tile("6c"))] * 4
        + [(0, Tile("8d"))] * 4 + [(0, Tile("8b"))] * 4
    )
    state, _ = G.discard(state, 1, Tile.JOKER, CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    state, _ = G.force_fallow(state, 1, FALLOW_VALUE_CARD, rng())
    assert state.outcome is not None
    assert state.outcome.value == 50  # the joker route does not exist


@pytest.mark.parametrize(
    "rack0, discard, discounted",
    [
        # two matching rack tiles: the CALL route is legal → takeable
        ("flower*4 1d*4 6b*2 8c*2 9b", "6b", True),
        # one matching + one joker also stands the call up
        ("flower*4 1d*4 6b joker 8c*2 9b", "6b", True),
        # one matching, no joker, not hand-completing: no route → not takeable
        ("flower*4 1d*4 6b*3 8c 9b", "8c", False),
        # a discarded joker is unclaimable by anyone, ever
        ("flower*4 1d*4 6b*3 8c*2", "joker", False),
        # hand-completing tile: the MAHJONG route applies even with pool < 2
        ("flower*4 1d*4 6b*4 8c", "8c", True),
    ],
    ids=["call-pool-2", "call-with-joker", "no-route", "joker", "mahjong-route"],
)
def test_obtainable_seen_discounts_only_legal_routes(rack0, discard, discounted):
    # V1: "takeable" means a LEGAL route — instant mahjong, or a call stood
    # up by two matching rack tiles (naturals or jokers, §2.5). Anything
    # else, the live discard stays counted as gone.
    state = play_state(2, {0: rack0, 1: f"9c*12 {discard}"}, turn=1)
    # The same card the routes are judged against below: whether a seat is
    # passed for automatically depends on the card, so dealing the discard
    # with a different one would hide the mahjong route this is testing.
    state, _ = G.discard(state, 1, Tile(discard), FALLOW_VALUE_CARD)
    assert state.phase is Phase.CLAIM_WINDOW
    raw = G.seen_elsewhere(state, 0)
    adjusted = G.obtainable_seen(state, 0, FALLOW_VALUE_CARD)
    tile = Tile(discard)
    if discounted:
        assert adjusted.get(tile, 0) == raw[tile] - 1
    else:
        assert adjusted.get(tile, 0) == raw[tile]


@pytest.mark.parametrize("mode, shown", [("target", 3), ("gap", 3), ("coach", 1)])
def test_assist_shows_the_mode_s_line_count(mode, shown):
    # Coach shows ONE line (live QA: three was a wall of text mid-turn).
    state = play_state(2, {0: "flower*2 1d*2 9d 2b*3 wn*2 dr 5c*2", 1: "9c*13"})
    r = G.assist_readout(state, 0, CARD, mode)
    assert r is not None
    assert len(r.prospects) == min(shown, r.live_count)


def test_coach_suggestion_never_contradicts_the_ONE_shown_hand():
    # The count is shared with suggest_discard's intersection: reasoning
    # over three hands while showing one could suggest a tile the shown
    # hand still needs.
    import random as _r

    for seed in range(40):
        rng = _r.Random(seed)
        wall = build_deck()
        rng.shuffle(wall)
        state = play_state(2, {1: "9c*13"})
        state.seats[0].rack = wall[:13]
        r = G.assist_readout(state, 0, CARD, "coach")
        if r is None or r.suggestion is None:
            continue
        assert len(r.prospects) == 1
        assert r.suggestion not in dict(r.prospects[0].needed)
        assert r.suggestion in dict(r.prospects[0].dead_weight)


# ── Coach's claim-window verdict (live QA request 2026-08-23) ────────────────


def _claim_state(rack0: str, discard: str, rack1: str = "9c*12"):
    state = play_state(2, {0: rack0, 1: f"{rack1} {discard}"}, turn=1)
    state, _ = G.discard(state, 1, Tile(discard))
    assert state.phase is Phase.CLAIM_WINDOW
    return state


def test_coach_flags_a_winning_discard():
    # gh-1 one 8c short, and the 8c is live: take it.
    state = _claim_state("flower*4 2d*4 6b*4 8c", "8c")
    r = G.assist_readout(state, 0, CARD, "coach")
    assert r is not None
    assert r.claim_win is not None and r.claim_win.id == "gh-1"
    assert r.claim_call is None          # winning beats calling


def test_coach_advises_calling_when_it_shortens_the_line():
    # holds three 2d for gh-1's kong; the fourth is live — calling exposes
    # the kong and strictly shortens the hand
    state = _claim_state("flower*4 2d*3 6b*4 8c 9b", "2d")
    r = G.assist_readout(state, 0, CARD, "coach")
    assert r is not None
    assert r.claim_win is None
    assert r.claim_call is not None
    assert r.claim_call.count == 4                       # a kong
    assert list(r.claim_call.tiles) == [Tile("2d")] * 3
    assert r.claim_call.distance < r.claim_call.distance_before
    # and the engine accepts exactly the tiles coach named
    state2, _ = G.claim(state, 0, "call", list(r.claim_call.tiles),
                        CARD, rng())
    assert state2.seats[0].exposures


def test_coach_stays_quiet_on_a_useless_discard_or_a_joker():
    for discard in ("5c", "joker"):
        state = _claim_state("flower*4 2d*4 6b*2 9d wn 1b", discard)
        r = G.assist_readout(state, 0, CARD, "coach")
        assert r is not None
        assert r.claim_win is None and r.claim_call is None


def test_claim_advice_is_coach_only_and_never_for_the_discarder():
    state = _claim_state("flower*4 2d*4 6b*4 8c", "8c")
    for mode in ("target", "gap"):
        r = G.assist_readout(state, 0, CARD, mode)
        assert r is not None and r.claim_win is None and r.claim_call is None
    # the discarder can't claim their own tile
    r = G.assist_readout(state, 1, CARD, "coach")
    assert r is None or (r.claim_win is None and r.claim_call is None)


def test_claim_advice_stops_once_the_seat_has_responded():
    state = _claim_state("flower*4 2d*4 6b*4 8c", "8c")
    assert G.assist_readout(state, 0, CARD, "coach").claim_win is not None
    state.claims = {0: ("pass", [])}
    r = G.assist_readout(state, 0, CARD, "coach")
    assert r is not None and r.claim_win is None and r.claim_call is None


# ── Wall joker dial (live-feedback 2026-08-23) ───────────────────────────────


def test_deck_grows_with_extra_jokers_and_naturals_are_untouched():
    from collections import Counter as _C

    from bot_modules.games.mahjong.tiles import build_deck as bd

    standard, rich = _C(bd()), _C(bd(jokers=14))
    assert sum(standard.values()) == 152
    assert sum(rich.values()) == 158          # extra jokers ADD to the wall
    assert rich[Tile.JOKER] == 14
    for tile in Tile:
        if tile is not Tile.JOKER:
            assert rich[tile] == standard[tile]   # naturals never displaced


def test_the_matcher_counts_the_walls_real_joker_supply():
    # A line needing 10 jokers is dead under a standard wall and live under
    # a 14-joker one. Without threading the count, the coach would call a
    # perfectly live line dead (and the fallow payout would misprice it).
    from collections import Counter as _C

    from bot_modules.games.mahjong.match_logic import reachable_lines

    # flowers and the standard eight jokers are all gone, so gh-1's flower
    # kong can only be filled by jokers a bigger wall would still hold
    rack = tiles("2d*4 6b*4 8c*2 9c*4")
    seen = _C({Tile.JOKER: 8, Tile.FLOWER: 8})
    standard = {h.id for h in reachable_lines(rack, [], CARD, seen)}
    richer = {h.id for h in reachable_lines(
        rack, [], CARD, seen, joker_copies=20)}
    assert "gh-1" not in standard and "gh-1" in richer
    assert richer > standard                    # the extra jokers revive lines


def test_wall_jokers_ride_the_table_config_and_survive_a_restart():
    state = G.create_table(
        G.TableConfig(seat_count=2, wall_jokers=14), 100)
    state, _ = G.join_table(state, 101)
    reloaded = G.state_from_dict(G.state_to_dict(state))
    assert reloaded.config.wall_jokers == 14
    # a table dealt before the dial existed reads as a standard wall
    raw = G.state_to_dict(state)
    del raw["config"]["wall_jokers"]
    assert G.state_from_dict(raw).config.wall_jokers == 8


def test_a_dealt_wall_actually_carries_the_extra_jokers():
    from bot_modules.games.mahjong.tiles import shuffled_wall

    state = G.create_table(G.TableConfig(seat_count=2, wall_jokers=16), 100)
    state, _ = G.join_table(state, 101)
    state, _ = G.deal(state, shuffled_wall(rng(), jokers=16))
    dealt = sum(1 for s in state.seats for t in s.rack if t is Tile.JOKER)
    assert dealt + state.wall.count(Tile.JOKER) == 16
    assert total_tiles(state) == 152 + 8        # eight extra jokers in play
