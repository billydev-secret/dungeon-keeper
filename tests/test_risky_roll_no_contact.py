"""No-contact enforcement for Risky Rolls.

Risky Rolls pairs people with public dice, so it has no private moment
between the pairing and the contact — the room watched the roll that
decided it. The gate therefore acts on the *draw*: a value that would put a
no-contact pair in touch is redrawn before anyone sees it, so there is no
refusal to make indistinguishable from a success. What cannot be dodged
that way (a round that is only the two of them) is refused at close with
the ordinary "at least 2 players must roll".

See docs/no_contact_spec.md, "Risky Rolls: the dice are nudged, not the
outcome".
"""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from bot_modules.services.risky_roll.logic import (
    build_main_prompt_state,
    build_one_rule_prompt_state,
    choose_roll,
    has_blocked_edge,
    possible_directed_edges,
)
from bot_modules.services.risky_roll.models import PromptKind, RiskyRollState

ANN, BOB, CID, DEE, EVE = 1, 2, 3, 4, 5


def pair(a: int, b: int) -> set[tuple[int, int]]:
    return {(min(a, b), max(a, b))}


def _state(rolls: dict[int, int]) -> RiskyRollState:
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=ANN)
    s.rolls = dict(rolls)
    return s


def actual_edges(rolls: dict[int, int]) -> set[tuple[int, int]]:
    """The (asker, answerer) pairs a real resolution produces for *rolls*."""
    state = _state(rolls)
    resolution = state.resolve()
    edges: set[tuple[int, int]] = set()

    main = build_main_prompt_state("g", state, resolution.result_type)
    if main is not None and main.prompt_kind != PromptKind.ROOM:
        edges |= {(main.winner_id, t) for t in main.participant_user_ids}

    one = build_one_rule_prompt_state("g", state)
    if one is not None:
        edges |= {
            (asker, t)
            for asker in one.allowed_questioners()
            for t in one.participant_user_ids
        }
    return {(a, b) for a, b in edges if a != b}


# ── the contract: the predicate never under-reports ───────────────────


@pytest.mark.parametrize("seed", range(300))
def test_predicted_edges_cover_every_edge_a_real_resolution_produces(seed):
    """The one test that stops `possible_directed_edges` drifting from `resolve`.

    The predicate restates the seat rules rather than running them, because it
    has to answer before the hidden tie roll-offs happen. That is a drift risk,
    and this is what pins it: resolve the round for real and assert the edges
    it actually produced were all predicted.
    """
    rng = random.Random(seed)
    players = rng.sample([ANN, BOB, CID, DEE, EVE], rng.randint(2, 5))
    # A narrow value range makes ties — and therefore roll-offs — common.
    rolls = {uid: rng.choice([1, 1, 50, 69, 99, 100, 100]) for uid in players}

    predicted = possible_directed_edges(rolls)
    assert actual_edges(rolls) <= predicted, f"rolls={rolls}"


# ── the four contact moments ──────────────────────────────────────────


def test_ordinary_round_pairs_winner_with_loser():
    assert (CID, ANN) in possible_directed_edges({ANN: 10, BOB: 50, CID: 90})


def test_the_100_rule_also_pairs_the_winner_with_the_second_lowest():
    edges = possible_directed_edges({ANN: 20, BOB: 10, CID: 100})
    assert (CID, BOB) in edges  # lowest
    assert (CID, ANN) in edges  # second-lowest, pulled in by the 100


def test_the_1_rule_also_pairs_the_second_highest_with_the_loser():
    edges = possible_directed_edges({ANN: 1, BOB: 50, CID: 90})
    assert (CID, ANN) in edges  # winner asks
    assert (BOB, ANN) in edges  # second-highest asks too, pulled in by the 1


def test_a_69_makes_the_round_edgeless():
    # The 69 winner asks the room in a thread. A question put to everyone is
    # not directed contact, so there is nothing here for the gate to prevent.
    assert possible_directed_edges({ANN: 69, BOB: 50, CID: 90}) == set()


def test_a_round_too_small_to_resolve_has_no_edges():
    assert possible_directed_edges({ANN: 50}) == set()


# ── pessimism about ties ──────────────────────────────────────────────


def test_a_tie_for_highest_predicts_every_possible_winner():
    # The roll-off has not run yet, so either tied player could be asking.
    edges = possible_directed_edges({ANN: 90, BOB: 90, CID: 10})
    assert (ANN, CID) in edges
    assert (BOB, CID) in edges


def test_a_tie_for_lowest_predicts_every_possible_loser():
    edges = possible_directed_edges({ANN: 10, BOB: 10, CID: 90})
    assert (CID, ANN) in edges
    assert (CID, BOB) in edges


# ── has_blocked_edge ──────────────────────────────────────────────────


def test_blocked_edge_is_found_in_either_direction():
    rolls = {ANN: 10, BOB: 90}
    # ANN answers BOB here; the pair is stored low-first regardless.
    assert has_blocked_edge(rolls, pair(ANN, BOB)) is True
    assert has_blocked_edge(rolls, pair(BOB, ANN)) is True


def test_no_pairs_means_no_blocked_edge():
    assert has_blocked_edge({ANN: 10, BOB: 90}, set()) is False


def test_a_pair_that_is_not_seated_together_is_not_blocked():
    # ANN and BOB are a pair, but CID wins and ANN answers — BOB is a bystander.
    assert has_blocked_edge({ANN: 10, BOB: 50, CID: 90}, pair(ANN, BOB)) is False


# ── the nudge ─────────────────────────────────────────────────────────


def test_a_round_with_no_pairs_takes_the_honest_roll():
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=42):
        assert choose_roll({ANN: 10}, BOB, set()) == 42


def test_a_safe_natural_roll_is_kept_untouched():
    # ANN and BOB are a pair. CID rolling 95 takes the winner's seat off BOB,
    # so ANN answers CID and the pair are never seated together — the honest
    # roll is already safe and is kept.
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=95):
        assert choose_roll({ANN: 10, BOB: 90}, CID, pair(ANN, BOB)) == 95


def test_an_unsafe_natural_roll_is_redrawn_into_a_safe_one():
    rolls = {ANN: 10, BOB: 90}
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=50):
        # 50 leaves BOB winning and ANN answering — the blocked pairing.
        value = choose_roll(rolls, CID, pair(ANN, BOB))
    assert value != 50
    assert not has_blocked_edge({**rolls, CID: value}, pair(ANN, BOB))


def test_the_redraw_never_manufactures_a_69():
    # When the pair are the only players, 69 is the ONLY safe value — a room
    # question has no directed edge. Picking from the safe set would make the
    # second of them to roll come up 69 every time, which is a louder tell
    # than the thing being hidden. The round falls through to the close check.
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=50):
        assert choose_roll({ANN: 40}, BOB, pair(ANN, BOB)) == 50


def test_a_natural_69_is_still_kept():
    # 69 is excluded from the REDRAW pool, not from the honest draw.
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=69):
        assert choose_roll({ANN: 40}, BOB, pair(ANN, BOB)) == 69


def test_the_nudge_dodges_a_second_seat_collision_from_the_100_rule():
    # BOB is about to roll 100, which would drag ANN in as second-lowest.
    rolls = {ANN: 50, CID: 5}
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=100):
        value = choose_roll(rolls, BOB, pair(ANN, BOB))
    assert not has_blocked_edge({**rolls, BOB: value}, pair(ANN, BOB))


def test_the_nudge_dodges_a_second_seat_collision_from_the_1_rule():
    # ANN rolled 1, so the second-highest asks her too. A natural 93 would put
    # BOB in that seat; DEE at 92 gives the redraw somewhere to go.
    rolls = {ANN: 1, CID: 95, DEE: 92}
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=93):
        value = choose_roll(rolls, BOB, pair(ANN, BOB))
    assert value != 93
    assert not has_blocked_edge({**rolls, BOB: value}, pair(ANN, BOB))


def test_the_1_rule_seat_can_be_unavoidable_with_nobody_left_to_take_it():
    # Same shape without DEE: BOB is the only player who can be second-highest,
    # so every value seats him asking ANN. The nudge gives up and returns the
    # honest roll; the close path is what refuses.
    rolls = {ANN: 1, CID: 95}
    with patch("bot_modules.services.risky_roll.logic.random.randint", return_value=90):
        assert choose_roll(rolls, BOB, pair(ANN, BOB)) == 90
    assert has_blocked_edge({**rolls, BOB: 90}, pair(ANN, BOB)) is True


@pytest.mark.parametrize("seed", range(200))
def test_a_later_roll_can_clear_a_collision_but_never_create_one(seed):
    """Why the nudge only has to look at the round in front of it.

    A new roll can push the maximum up or the minimum down, but it can never
    *promote* someone already in the round into either seat; the same holds
    for the second seats, where an added value can only pull the seat away
    from whoever holds it. So a pairing that is safe when the second member
    of a pair rolls stays safe however the rest of the round fills up — and
    a doomed one can still be rescued.

    That is what lets `choose_roll` decide against the current roll set alone
    instead of having to anticipate everyone who has not rolled yet.
    """
    rng = random.Random(seed)
    blocked = pair(ANN, BOB)
    rolls = {
        ANN: rng.randint(1, 100),
        BOB: rng.randint(1, 100),
        CID: rng.randint(1, 100),
    }
    if has_blocked_edge(rolls, blocked):
        return  # already doomed; this property is about safe rounds staying safe

    for newcomer, value in ((DEE, rng.randint(1, 100)), (EVE, rng.randint(1, 100))):
        rolls[newcomer] = value
        assert not has_blocked_edge(rolls, blocked), f"rolls={rolls}"


# ── the residual: a round that cannot be made safe ────────────────────


def test_a_round_of_only_the_pair_cannot_be_made_safe():
    # Whatever BOB rolls, one of them is highest and the other lowest. There
    # is no safe value, so the nudge returns the honest roll and the close
    # path refuses. Every value is checked, so this is exhaustive.
    for value in range(1, 101):
        if value == 69:
            continue  # a 69 turns it into a room question
        assert has_blocked_edge({ANN: 40, BOB: value}, pair(ANN, BOB)) or value == 40


def test_the_pair_can_be_rescued_by_a_later_third_roll():
    # ANN and BOB roll adjacent values with nobody else in the round: doomed
    # at that moment. CID then rolls high and the round is safe again — which
    # is why the close-time check is authoritative and the nudge is not.
    doomed = {ANN: 50, BOB: 60}
    assert has_blocked_edge(doomed, pair(ANN, BOB)) is True
    assert has_blocked_edge({**doomed, CID: 90}, pair(ANN, BOB)) is False
