"""Random derangements, with an optional set of pairs that must never meet.

The unconstrained path is Sattolo's cycle — one shuffle, guaranteed no fixed
point. The constrained path exists for the no-contact list: a caller hands in
the ``(low, high)`` pairs ``no_contact_pairs_among`` returns for the pool, and
no such pair may be giver→receiver in *either* direction. That is a matching
problem, so it is solved constructively (randomised backtracking with a
forward check) rather than by rejecting Sattolo draws: a bounded retry can
miss a valid arrangement that Sattolo's single-cycle shape can never produce,
and it cannot tell "unlucky" from "impossible". When no valid derangement
exists the helper returns ``{}`` — the same answer as a too-small pool — so
the caller refuses with its ordinary copy and the protected member can't tell.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

Pair = tuple[int, int]


def _normalise(forbidden: Iterable[Pair] | None) -> set[Pair]:
    """``(low, high)`` every pair, so a caller's ordering never matters."""
    if not forbidden:
        return set()
    return {(a, b) if a < b else (b, a) for a, b in forbidden}


def _sattolo(participants: list[int], chooser: random.Random) -> dict[int, int]:
    n = len(participants)
    shuffled = participants[:]
    chooser.shuffle(shuffled)
    perm = list(range(n))
    for i in range(n - 1, 0, -1):
        j = chooser.randint(0, i - 1)
        perm[i], perm[j] = perm[j], perm[i]
    return {shuffled[i]: shuffled[perm[i]] for i in range(n)}


# The backtracking search's node budget. The forward check only proves each
# remaining giver has *some* free receiver (not Hall's condition), so a
# pathological forbidden set — several givers who can only reach the same
# few receivers — is discovered deep in the tree and would otherwise
# enumerate the free givers' permutations. Real pools are small and their
# blocked pairs few; the budget is the guarantee, not the expectation.
MAX_SEARCH_NODES = 20_000


def _constrained(
    participants: list[int],
    forbidden: set[Pair],
    chooser: random.Random,
    *,
    max_nodes: int = MAX_SEARCH_NODES,
) -> dict[int, int]:
    """Randomised backtracking over receivers; ``{}`` when nothing fits —
    or when ``max_nodes`` runs out, which the caller treats the same way."""
    givers = participants[:]
    chooser.shuffle(givers)
    budget = [max_nodes]

    def allowed(giver: int, receiver: int) -> bool:
        if giver == receiver:
            return False
        key = (giver, receiver) if giver < receiver else (receiver, giver)
        return key not in forbidden

    assigned: dict[int, int] = {}
    free: set[int] = set(participants)

    def solve(index: int) -> bool:
        if index == len(givers):
            return True
        budget[0] -= 1
        if budget[0] < 0:
            return False
        giver = givers[index]
        # Forward check: every giver still to place must have somewhere to
        # go among the receivers still free, or this branch is already dead.
        for later in givers[index:]:
            if not any(allowed(later, r) for r in free):
                return False
        candidates = [r for r in free if allowed(giver, r)]
        chooser.shuffle(candidates)
        for receiver in candidates:
            assigned[giver] = receiver
            free.remove(receiver)
            if solve(index + 1):
                return True
            free.add(receiver)
            del assigned[giver]
        return False

    if not solve(0):
        return {}
    # Return in the caller's participant order, matching the Sattolo path.
    return {p: assigned[p] for p in participants}


def random_derangement(
    participants: list[int],
    forbidden: Iterable[Pair] | None = None,
    *,
    rng: random.Random | None = None,
    max_nodes: int = MAX_SEARCH_NODES,
) -> dict[int, int]:
    """
    Generate a random derangement: each person gives to exactly one other,
    receives from exactly one other, and no one is paired with themselves.
    Returns {giver_id: receiver_id}.

    ``forbidden`` is a set of ``(low, high)`` user-id tuples (the shape
    ``no_contact_pairs_among`` returns); a forbidden pair is never paired in
    either direction. Returns ``{}`` for fewer than two participants, and
    also when the forbidden pairs leave no valid derangement at all —
    indistinguishable on purpose, so the caller's "too few players"
    refusal covers both.

    ``rng`` is injectable for deterministic tests; defaults to ``random``.
    ``max_nodes`` bounds the constrained search (see ``MAX_SEARCH_NODES``).

    The constrained path is CPU work: call it from a worker thread
    (``asyncio.to_thread``) when the caller is on the event loop.
    """
    if len(participants) < 2:
        return {}
    chooser = rng if rng is not None else random.Random(random.random())
    banned = _normalise(forbidden)
    if not banned:
        return _sattolo(participants, chooser)
    return _constrained(participants, banned, chooser, max_nodes=max_nodes)
