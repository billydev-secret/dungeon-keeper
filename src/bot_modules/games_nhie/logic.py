"""Pure decision logic for the Never Have I Ever cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and the round-advance coroutine; the Discord glue
(sending the message, persisting via ``update_game_payload``) stays in
the cog.

Three reusable spines are extracted:

* :func:`apply_vote` — the guilty/innocent toggle, including the
  "switching sides" bookkeeping and the lazy "register this player in
  the lives tracker on their first vote" behaviour. Mirrors the
  ``vote_guilty``/``vote_innocent`` button callbacks.
* :func:`apply_round_lives` — at round-advance time, deduct a heart
  from each guilty voter, mark new eliminations, and lazily register
  innocent voters who weren't seen before. This is the spine the cog
  copies into its ``advance`` closure.
* :func:`find_winner` — game-over detection: returns a tagged
  ``("winner", uid)`` / ``("all_eliminated", None)`` / ``("continue",
  None)`` so the cog branches cleanly.

The remaining helpers (:func:`bump_guilt_scores`,
:func:`payload_to_round_state`) are tiny dict transforms the cog used
to inline; pulling them out makes the cog's advance closure linear and
the bookkeeping testable.
"""

from __future__ import annotations

from typing import Any, Literal

DEFAULT_LIVES = 3

VoteKind = Literal["guilty", "innocent"]
WinnerStatus = Literal["winner", "all_eliminated", "continue"]


def apply_vote(
    guilty: list[int],
    innocent: list[int],
    lives: dict[int, int],
    uid: int,
    vote_kind: VoteKind,
    max_lives: int = DEFAULT_LIVES,
) -> bool:
    """Apply ``uid``'s ``vote_kind`` vote to the two side-lists.

    Mutates ``guilty``, ``innocent``, and ``lives`` in place. Assumes
    the caller has already gated out eliminated players (the cog's
    button callback handles the ephemeral "you've been eliminated"
    reply before calling this).

    Behaviour:

    * If the user is on the opposite list, they're moved off it
      (the "changed sides" path) and the returned flag is ``True``.
    * If the user is already on the chosen list, this is a no-op
      idempotent re-press; ``False`` is returned.
    * On any vote (new or switch) the user is lazily registered in
      ``lives`` with a full bar when ``max_lives > 0``.

    Returns ``True`` if the user switched sides, ``False`` otherwise
    (used by the cog to add a ``" (changed)"`` suffix to the ephemeral
    confirmation).
    """
    if vote_kind == "guilty":
        chosen, other = guilty, innocent
    else:
        chosen, other = innocent, guilty

    changed = uid in other
    if changed:
        other.remove(uid)
    if uid not in chosen:
        chosen.append(uid)
    if max_lives > 0 and uid not in lives:
        lives[uid] = max_lives
    return changed


def apply_round_lives(
    lives: dict[int, int],
    eliminated: set[int],
    guilty: list[int],
    innocent: list[int],
    max_lives: int,
) -> list[int]:
    """Resolve a finished round's life deductions and eliminations.

    Mutates ``lives`` and ``eliminated`` in place. Two distinct things
    happen here (mirroring the cog's ``advance`` closure):

    1. Each guilty voter loses a heart (unless already eliminated). If
       a heart-count drops to zero or below, the player is moved into
       ``eliminated`` and appended to the returned list so the cog can
       announce the death publicly.
    2. Each innocent voter who isn't tracked yet (and isn't eliminated)
       is lazily registered with ``max_lives``. This is easy to miss
       but matters for "still standing" rendering on subsequent rounds.

    When ``max_lives <= 0`` (elimination disabled), this is a no-op and
    an empty list is returned.
    """
    newly_eliminated: list[int] = []
    if max_lives <= 0:
        return newly_eliminated

    for uid in guilty:
        if uid in eliminated:
            continue
        if uid not in lives:
            lives[uid] = max_lives
        lives[uid] -= 1
        if lives[uid] <= 0:
            eliminated.add(uid)
            newly_eliminated.append(uid)

    for uid in innocent:
        if uid not in lives and uid not in eliminated:
            lives[uid] = max_lives

    return newly_eliminated


def find_winner(
    lives: dict[int, int],
    eliminated: set[int],
) -> tuple[WinnerStatus, int | None]:
    """Return a tagged game-state after a round resolves.

    * ``("continue", None)`` when more than one player is still alive,
      or when ``lives`` is empty (no one's been tracked yet — the cog
      treats this as "keep playing").
    * ``("winner", uid)`` when exactly one player remains alive.
    * ``("all_eliminated", None)`` when no one is alive (the rare
      simultaneous-knockout case).

    A player is alive when their ``lives`` entry is positive AND they
    are not in ``eliminated``.
    """
    if not lives:
        return ("continue", None)
    alive = [uid for uid, hp in lives.items() if hp > 0 and uid not in eliminated]
    if len(alive) > 1:
        return ("continue", None)
    if len(alive) == 1:
        return ("winner", alive[0])
    return ("all_eliminated", None)


def bump_guilt_scores(
    guilt_scores: dict[str, int], guilty_ids: list[int]
) -> None:
    """Increment per-user guilty counts after a round resolves.

    Mutates ``guilt_scores`` in place. Keys are stringified user IDs to
    match Discord's payload-serialisation convention.
    """
    for uid in guilty_ids:
        key = str(uid)
        guilt_scores[key] = guilt_scores.get(key, 0) + 1


def payload_to_round_state(
    payload: dict[str, Any],
) -> tuple[dict[int, int], set[int], int]:
    """Decode the persisted payload into typed round-state.

    The on-disk payload stores ``lives`` keys as strings and
    ``eliminated`` as a list of strings (Discord-side JSON convention).
    The cog needs typed dicts/sets to mutate. This helper does the
    conversion once so the cog doesn't repeat the comprehensions in
    every place it reads the payload.

    Returns ``(lives, eliminated, max_lives)``.
    """
    lives_raw = payload.get("lives", {})
    lives = {int(k): int(v) for k, v in lives_raw.items()}
    eliminated = {int(x) for x in payload.get("eliminated", [])}
    max_lives = int(payload.get("max_lives", DEFAULT_LIVES))
    return lives, eliminated, max_lives


def encode_round_state(
    lives: dict[int, int], eliminated: set[int]
) -> tuple[dict[str, int], list[str]]:
    """Encode typed round-state back to payload-friendly primitives.

    Inverse of :func:`payload_to_round_state`. Used by the cog when
    writing the resolved state back to the persisted payload.
    """
    return (
        {str(k): v for k, v in lives.items()},
        [str(x) for x in eliminated],
    )
