"""The AI seat's brain — stage 1 of docs/plans/mahjong-bots.md.

Per-decider legality and quality on crafted states, then the proof that
matters: seeded bot-vs-bot games driven through the REAL engine to
completion at both seat counts — every game ends in a mahjong or a wall
game, no exception, no stalled phase. The loop mirrors the service's act
dispatch exactly, so a BotAction the service couldn't apply fails here.
"""

from __future__ import annotations

import random

import pytest

import bot_modules.games.mahjong.game_logic as G
from bot_modules.games.mahjong.bot_logic import (
    BotAction,
    bot_member_id,
    decide,
    is_bot_id,
)
from bot_modules.games.mahjong.card_logic import load_first_light
from bot_modules.games.mahjong.game_logic import Phase, TableConfig
from bot_modules.games.mahjong.tiles import Tile, build_deck
from tests.test_mahjong_game_logic import play_state, tiles

CARD = load_first_light()


# ── Synthetic ids (plan B3) ──────────────────────────────────────────────────


def test_bot_ids_are_negative_and_collision_free():
    ids = {bot_member_id(t, s) for t in range(1, 200) for s in range(4)}
    assert len(ids) == 199 * 4          # unique across tables and seats
    assert all(i < 0 for i in ids)      # can never collide with a snowflake
    assert all(is_bot_id(i) for i in ids)
    assert not is_bot_id(100)


# ── Own turn ─────────────────────────────────────────────────────────────────


def test_bot_declares_a_won_hand():
    state = play_state(2, {0: "flower*4 2d*4 6b*4 8c*2", 1: "9c*13"})
    action = decide(state, 0, CARD, random.Random(0))
    assert action == BotAction("mahjong")


def test_bot_discards_dead_weight_not_line_tiles():
    state = play_state(2, {0: "flower*4 2d*4 6b*2 9d wn 1b", 1: "9c*13"})
    action = decide(state, 0, CARD, random.Random(0))
    assert action is not None and action.action == "discard"
    tile = action.kwargs["tile"]
    # gh-1 (the closest line) holds F*4 2d*4 6b*2 — none of those may go
    assert tile not in {Tile("flower"), Tile("2d"), Tile("6b")}
    # and the discard is legal: actually in the rack
    assert tile in state.seats[0].rack
    state2, _ = G.discard(state, 0, tile)
    assert state2.phase is Phase.CLAIM_WINDOW


def test_bot_never_discards_a_joker():
    # joker-heavy rack of scattered singles — no line matches (every pair
    # wants two naturals; the all-3+ quint lines want 11 jokers, not 6),
    # so the fallback runs and must still find a natural to throw
    state = play_state(2, {0: "joker*6 1d 3d 5d 7d 9d 2b 4b 6b", 1: "9c*13"})
    action = decide(state, 0, CARD, random.Random(0))
    assert action is not None and action.action == "discard"
    assert action.kwargs["tile"] is not Tile.JOKER


def test_bot_redeems_only_a_spare_natural():
    # Seat 1's exposure holds a joker impersonating 9d. Holding a spare 9d
    # (useless to every close line) → redeem; holding a 2d the best line
    # wants → leave it alone and discard instead.
    exposures = {1: [G.ExposureState(exposure_id=7, natural=Tile("9d"),
                                     count=4, jokers=1)]}
    spare = play_state(
        2, {0: "flower*4 2d*4 6b*3 9d wn", 1: "9c*9"}, exposures=exposures)
    action = decide(spare, 0, CARD, random.Random(0))
    assert action == BotAction(
        "redeem_joker", {"exposure_id": 7, "tile": Tile("9d")})
    # the same shape with no spare: no redemption, a discard comes out
    wanted = play_state(
        2, {0: "flower*4 2d*4 6b*3 8c*2 wn", 1: "9c*9"}, exposures=exposures)
    action = decide(wanted, 0, CARD, random.Random(0))
    assert action is not None and action.action == "discard"


# ── Claim window ─────────────────────────────────────────────────────────────


def _window(racks, discard_tile, *, discarder=1, pit=""):
    state = play_state(2, racks, turn=discarder)
    if pit:
        state.discards = [(discarder, t) for t in tiles(pit)]
    state, _ = G.discard(state, discarder, Tile(discard_tile))
    assert state.phase is Phase.CLAIM_WINDOW
    return state


def test_bot_claims_a_winning_discard():
    state = _window({0: "flower*4 2d*4 6b*4 8c", 1: "9c*12 8c"}, "8c")
    action = decide(state, 0, CARD, random.Random(0))
    assert action == BotAction("claim", {"kind": "mahjong", "tiles": []})


def test_bot_calls_when_the_exposure_advances_the_best_line():
    # gh-1 wants a 2d kong; bot holds 3 naturals, the 4th is live: calling
    # exposes the kong and strictly shortens the line.
    state = _window({0: "flower*4 2d*3 6b*4 8c 9b", 1: "9c*12 2d"}, "2d")
    action = decide(state, 0, CARD, random.Random(0))
    assert action is not None and action.action == "claim"
    assert action.kwargs["kind"] == "call"
    given = action.kwargs["tiles"]
    assert given == [Tile("2d")] * 3
    # and the engine accepts exactly this call
    state2, _ = G.claim(state, 0, "call", given, CARD, random.Random(0))
    assert state2.seats[0].exposures


def test_bot_passes_a_useless_discard_and_any_joker():
    state = _window({0: "flower*4 2d*4 6b*2 9d wn 1b", 1: "9c*12 5c"}, "5c")
    action = decide(state, 0, CARD, random.Random(0))
    assert action == BotAction("claim", {"kind": "pass", "tiles": []})
    state = _window({0: "flower*4 2d*4 6b*2 9d wn 1b", 1: "9c*12 joker"}, "joker")
    action = decide(state, 0, CARD, random.Random(0))
    assert action == BotAction("claim", {"kind": "pass", "tiles": []})


def test_bot_never_acts_out_of_turn_or_when_fallow():
    state = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=1)
    assert decide(state, 0, CARD, random.Random(0)) is None  # not their turn
    state.seats[0].fallow = True
    state.phase = Phase.CLAIM_WINDOW
    assert decide(state, 0, CARD, random.Random(0)) is None  # fallow


# ── Simultaneous phases ──────────────────────────────────────────────────────


def test_charleston_pick_is_three_legal_nonjokers():
    state = play_state(2, {0: "joker*2 flower*2 2d*2 9d 9b 9c wn we ws dr"})
    state.phase = Phase.CHARLESTON
    state.pending_picks = {}
    action = decide(state, 0, CARD, random.Random(0))
    assert action is not None and action.action == "charleston_pick"
    picks = action.kwargs["tiles"]
    assert len(picks) == 3 and Tile.JOKER not in picks
    assert action.kwargs["blind_n"] == 0
    for t in picks:
        assert t in state.seats[0].rack


def test_vote_follows_the_mode():
    state = play_state(2, {0: "9c*13"})
    state.phase = Phase.CHARLESTON_VOTE
    assert decide(state, 0, CARD, random.Random(0), practice=True) == BotAction(
        "vote", {"yes": True})
    assert decide(state, 0, CARD, random.Random(0), practice=False) == BotAction(
        "vote", {"yes": False})


def test_courtesy_proposes_zero_and_gives_worst():
    state = play_state(2, {0: "flower*4 2d*4 6b*2 9d wn 1b"})
    state.phase = Phase.COURTESY_PROPOSE
    assert decide(state, 0, CARD, random.Random(0)) == BotAction(
        "courtesy_propose", {"n": 0})
    state.phase = Phase.COURTESY_PICK
    state.courtesy_owed = {0: 2}
    action = decide(state, 0, CARD, random.Random(0))
    assert action is not None and action.action == "courtesy_pick"
    given = action.kwargs["tiles"]
    assert len(given) == 2 and Tile.JOKER not in given
    assert set(given) <= set(state.seats[0].rack)
    assert set(given).isdisjoint({Tile("flower"), Tile("2d")})  # line tiles stay


def test_rematch_follows_humans_never_leads():
    state = play_state(2, {0: "9c*13", 1: "8b*13"})
    state.seats[1].member_id = bot_member_id(1, 1)
    state.phase = Phase.SETTLE
    state.rematch_votes = set()
    assert decide(state, 1, CARD, random.Random(0)) is None       # human undecided
    state.rematch_votes = {0}
    assert decide(state, 1, CARD, random.Random(0)) == BotAction("rematch")


# ── The proof: full games, real engine, both seat counts ─────────────────────

_ACTIONS = {
    "charleston_pick": lambda st, seat, kw, rng: G.charleston_pick(
        st, seat, kw["tiles"], kw["blind_n"], rng),
    "vote": lambda st, seat, kw, rng: G.vote_second_charleston(
        st, seat, kw["yes"]),
    "courtesy_propose": lambda st, seat, kw, rng: G.courtesy_propose(
        st, seat, kw["n"]),
    "courtesy_pick": lambda st, seat, kw, rng: G.courtesy_pick(
        st, seat, kw["tiles"]),
    "discard": lambda st, seat, kw, rng: G.discard(st, seat, kw["tile"]),
    "claim": lambda st, seat, kw, rng: G.claim(
        st, seat, kw["kind"], kw.get("tiles", []), CARD, rng),
    "redeem_joker": lambda st, seat, kw, rng: G.redeem_joker(
        st, seat, kw["exposure_id"], kw["tile"]),
    "mahjong": lambda st, seat, kw, rng: G.declare_mahjong_own_turn(
        st, seat, CARD),
}


def _drive_full_game(seat_count: int, seed: int):
    """Bots at every seat; mirror the service dispatch until the hand ends."""
    rng = random.Random(seed)
    state = G.create_table(
        TableConfig(seat_count=seat_count, wall_trim=60 if seat_count == 2 else 0),
        bot_member_id(1, 0),
    )
    for s in range(1, seat_count):
        state, _ = G.join_table(state, bot_member_id(1, s))
    wall = build_deck()
    rng.shuffle(wall)
    state, _ = G.deal(state, wall)

    for step in range(3000):
        if state.phase in (Phase.SETTLE, Phase.CLOSED):
            return state, step
        acted = False
        for seat in range(seat_count):
            action = decide(state, seat, CARD, rng, practice=False)
            if action is None:
                continue
            state, _ = _ACTIONS[action.action](state, seat, action.kwargs, rng)
            acted = True
            break  # one action per iteration, like the real driver
        assert acted, (
            f"stalled in {state.phase} at step {step} "
            f"(seed {seed}, {seat_count} seats)"
        )
    pytest.fail(f"game never ended (seed {seed}, {seat_count} seats)")


@pytest.mark.parametrize("seat_count", [2, 4])
@pytest.mark.parametrize("seed", range(4))
def test_bots_finish_every_game(seat_count, seed):
    state, steps = _drive_full_game(seat_count, seed)
    assert state.phase is Phase.SETTLE
    assert state.outcome is not None
    assert state.outcome.kind in ("mahjong", "wall_game", "fallow_end")
    # a bot never times out, so no seat may have earned a strike
    assert all(s.strikes == 0 for s in state.seats)
    if state.outcome.kind == "mahjong":
        assert state.outcome.winner is not None


# ── Bots review round (2026-08-22): P5/P7 brain fixes ────────────────────────


def test_all_joker_rack_still_discards():
    # P5: heavy exposures can leave a rack of nothing but jokers; a bot must
    # still discard — a joker, as the last resort, rather than crash.
    from bot_modules.games.mahjong.game_logic import ExposureState

    exposures = {0: [
        ExposureState(exposure_id=1, natural=Tile("wn"), count=4, jokers=0),
        ExposureState(exposure_id=2, natural=Tile("2d"), count=4, jokers=0),
        ExposureState(exposure_id=3, natural=Tile("6b"), count=3, jokers=0),
    ]}
    state = play_state(2, {0: "joker*3", 1: "9c*13"}, exposures=exposures)
    action = decide(state, 0, CARD, random.Random(0))
    assert action is not None and action.action == "discard"
    assert action.kwargs["tile"] is Tile.JOKER   # nothing else to throw
    state2, _ = G.discard(state, 0, action.kwargs["tile"])
    assert state2.phase is Phase.CLAIM_WINDOW


def test_fallow_bot_still_follows_a_rematch():
    # P7: the engine accepts (and deal() resets) a fallow seat's rematch
    # vote; the blanket fallow guard used to sit before the SETTLE branch,
    # so a table with a folded bot hung at settle until expiry.
    state = play_state(2, {0: "9c*13", 1: "8b*13"})
    state.seats[1].member_id = bot_member_id(1, 1)
    state.seats[1].fallow = True
    state.phase = Phase.SETTLE
    state.rematch_votes = {0}                    # the human wants another
    action = decide(state, 1, CARD, random.Random(0))
    assert action == BotAction("rematch")
