"""The shared derangement helper, with and without forbidden pairs.

``random_derangement`` is the pairing engine behind Spin the Compliment: every
participant gives to exactly one other and receives from exactly one other,
never themselves. The ``forbidden`` argument is the no-contact hook — a set of
``(low, high)`` tuples in the shape ``no_contact_pairs_among`` returns — and a
forbidden pair must never appear in *either* direction. When the constraints
leave no valid derangement at all the helper returns ``{}``, which the cog
treats exactly like a too-small pool so the protected party can't tell.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games.utils.derangement import random_derangement


def _is_derangement(participants: list[int], pairings: dict[int, int]) -> bool:
    return (
        set(pairings) == set(participants)
        and sorted(pairings.values()) == sorted(participants)
        and all(g != r for g, r in pairings.items())
    )


def _violates(pairings: dict[int, int], forbidden: set[tuple[int, int]]) -> bool:
    return any(
        (min(g, r), max(g, r)) in forbidden for g, r in pairings.items()
    )


# ── the unconstrained contract (unchanged) ───────────────────────────────────


@pytest.mark.parametrize("pool", [[], [1]], ids=["empty", "single"])
def test_fewer_than_two_returns_empty(pool):
    assert random_derangement(pool) == {}


@pytest.mark.parametrize("size", [2, 3, 5, 12])
def test_every_participant_gives_and_receives_exactly_once(size):
    pool = list(range(100, 100 + size))
    for _ in range(25):
        assert _is_derangement(pool, random_derangement(pool))


def test_two_players_always_swap():
    assert random_derangement([1, 2]) == {1: 2, 2: 1}


# ── forbidden pairs: never paired, in either direction ───────────────────────


@pytest.mark.parametrize(
    ("pool", "forbidden"),
    [
        pytest.param([1, 2, 3, 4], {(1, 2)}, id="four-one-pair"),
        pytest.param([1, 2, 3, 4], {(1, 3), (2, 4)}, id="four-two-disjoint-pairs"),
        pytest.param([1, 2, 3, 4, 5], {(1, 2), (1, 3), (1, 4)}, id="one-blocked-from-most"),
        pytest.param(list(range(1, 13)), {(1, 2), (3, 4), (5, 6), (7, 8)}, id="twelve-four-pairs"),
    ],
)
def test_forbidden_pair_never_appears_in_either_direction(pool, forbidden):
    for seed in range(40):
        pairings = random_derangement(pool, forbidden, rng=random.Random(seed))
        assert _is_derangement(pool, pairings), pairings
        assert not _violates(pairings, forbidden), pairings


def test_forbidden_pair_is_read_symmetrically():
    """A ``(low, high)`` entry forbids both giver->receiver directions."""
    forbidden = {(1, 2)}
    for _ in range(40):
        pairings = random_derangement([1, 2, 3, 4], forbidden)
        assert pairings[1] != 2 and pairings[2] != 1


def test_forbidden_pairs_given_unordered_still_apply():
    """A caller that builds the tuple the other way round is still honoured."""
    for _ in range(40):
        pairings = random_derangement([1, 2, 3, 4], {(2, 1)})
        assert pairings[1] != 2 and pairings[2] != 1


def test_constrained_result_is_still_random():
    """The constrained path must not collapse to one fixed arrangement."""
    pool = [1, 2, 3, 4, 5, 6]
    seen = {
        tuple(sorted(random_derangement(pool, {(1, 2)}).items())) for _ in range(60)
    }
    assert len(seen) > 1


# ── no valid derangement: {} (the cog's ordinary "too few players" refusal) ──


@pytest.mark.parametrize(
    ("pool", "forbidden"),
    [
        pytest.param([1, 2], {(1, 2)}, id="two-blocked"),
        # Every derangement of three is a 3-cycle, and a 3-cycle joins every
        # pair in one direction — so ANY blocked pair empties a 3-pool.
        pytest.param([1, 2, 3], {(1, 2)}, id="three-any-pair"),
        pytest.param([1, 2, 3], {(1, 2), (1, 3)}, id="one-blocked-from-everyone"),
        pytest.param([1, 2, 3, 4], {(1, 2), (1, 3), (1, 4)}, id="four-one-isolated"),
    ],
)
def test_impossible_constraints_return_empty(pool, forbidden):
    assert random_derangement(pool, forbidden) == {}


def test_empty_forbidden_set_behaves_like_none():
    assert random_derangement([1, 2], set()) == {1: 2, 2: 1}
