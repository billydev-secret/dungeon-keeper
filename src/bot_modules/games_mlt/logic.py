"""Pure decision logic for the Most Likely To cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and the round-advance closure; the Discord glue
(sending the message, persisting via ``update_game_payload``) stays in
the cog.

MLT is closest in shape to Never Have I Ever — both are per-round
voting games where players cast a single vote and the round resolves
to a tally. The key MLT-specific difference is that each vote targets
another *player* (not a guilty/innocent flag), so the tally is keyed
on candidate user IDs rather than two side-lists, and the "winner" of
a round is the most-voted player (with ties producing co-winners who
each get a crown).

Five reusable spines are extracted:

* :func:`add_player` / :func:`remove_player` — idempotent lobby
  membership management.
* :func:`apply_vote` — record (or re-target) a single voter's pick,
  reporting whether they switched from a previous target so the cog
  can append ``" (changed)"`` to the ephemeral confirmation.
* :func:`tally_votes` — flatten the ``{voter: target}`` map into a
  ``{target: count}`` tally, ensuring every player is represented
  (including those who received zero votes).
* :func:`find_round_winners` — return the list of crown recipients
  (top vote-getters; the list has length > 1 only on a tie, and is
  empty when no votes were cast).
* :func:`bump_crowns` — increment per-user crown counts after a round.

A pair of codec helpers (:func:`encode_round_votes`,
:func:`stringify_crowns`) handles the str-keyed JSON payload shape
the games stack persists.
"""

from __future__ import annotations

# Minimum players required before the host can start the game.
MIN_PLAYERS = 3


def add_player(players: list[int], uid: int) -> bool:
    """Append ``uid`` to ``players`` if not already present.

    Mutates ``players`` in place. Returns ``True`` when the player was
    newly added, ``False`` for an idempotent re-press.
    """
    if uid in players:
        return False
    players.append(uid)
    return True


def remove_player(players: list[int], uid: int) -> bool:
    """Remove ``uid`` from ``players`` if present.

    Mutates ``players`` in place. Returns ``True`` when the player was
    actually removed, ``False`` for an idempotent no-op.
    """
    if uid not in players:
        return False
    players.remove(uid)
    return True


def can_start(players: list[int], min_players: int = MIN_PLAYERS) -> bool:
    """Return ``True`` when there are enough players to start the game.

    The cog gates the Start button on this; ``min_players`` defaults
    to :data:`MIN_PLAYERS` (3) which matches the cog's hard-coded
    threshold.
    """
    return len(players) >= min_players


def apply_vote(
    votes: dict[int, int],
    voter_id: int,
    target_id: int,
) -> bool:
    """Record ``voter_id``'s vote for ``target_id`` in ``votes``.

    Mutates ``votes`` in place. The caller is expected to have already
    gated out non-eligible voters (the cog's select-callback checks
    ``voter_id`` is in the player pool and returns an ephemeral error
    otherwise before calling this).

    Behavior:

    * If the voter has no prior vote, the new pick is recorded and
      ``False`` is returned.
    * If the voter is changing their pick, the previous target is
      overwritten and ``True`` is returned so the cog can append
      ``" (changed)"`` to its ephemeral confirmation.
    * Re-voting for the same target is a no-op switch (``False``).
    """
    prev = votes.get(voter_id)
    votes[voter_id] = target_id
    return prev is not None and prev != target_id


def tally_votes(
    votes: dict[int, int], players: list[int]
) -> dict[int, int]:
    """Aggregate per-voter picks into a per-candidate count.

    Every entry in ``players`` is represented in the result (with a
    count of zero when no one voted for them). Targets present in
    ``votes`` but missing from ``players`` are also included so a
    write-in vote (should one ever sneak past the cog's eligibility
    check) is still surfaced rather than silently dropped.
    """
    tally: dict[int, int] = {uid: 0 for uid in players}
    for target_id in votes.values():
        tally[target_id] = tally.get(target_id, 0) + 1
    return tally


def find_round_winners(tally: dict[int, int]) -> list[int]:
    """Return the player IDs tied for the most votes this round.

    * Empty list when ``tally`` is empty OR when the top score is 0
      (no votes cast — no crowns awarded).
    * Single-element list for a clear winner.
    * Two-or-more-element list on a tie, in iteration order of
      ``tally`` (stable for Python 3.7+).
    """
    if not tally:
        return []
    max_votes = max(tally.values())
    if max_votes <= 0:
        return []
    return [uid for uid, count in tally.items() if count == max_votes]


def bump_crowns(
    crowns: dict[str, int], winner_ids: list[int]
) -> None:
    """Increment per-user crown counts after a round resolves.

    Mutates ``crowns`` in place. Keys are stringified user IDs to
    match Discord's payload-serialisation convention.
    """
    for uid in winner_ids:
        key = str(uid)
        crowns[key] = crowns.get(key, 0) + 1


def encode_round_votes(votes: dict[int, int]) -> dict[str, int]:
    """Encode an in-memory ``{voter: target}`` map for the payload.

    Stringifies the voter keys (target IDs remain ints) to match the
    JSON-serialised shape persisted under ``payload['rounds'][N]
    ['votes']``.
    """
    return {str(voter): target for voter, target in votes.items()}


def queue_prompt(queued_prompts: list[str], prompt: str) -> int:
    """Append a non-empty prompt to ``queued_prompts``; return new count.

    Mirrors the modal's ``queued_prompts.append`` then label-rebuild
    flow. Leading/trailing whitespace is stripped; empty submissions
    are silently ignored (the cog's modal blocks empty input via the
    ``TextInput`` ``max_length`` constraint, but defending here keeps
    the logic resilient).
    """
    cleaned = prompt.strip()
    if cleaned:
        queued_prompts.append(cleaned)
    return len(queued_prompts)


def pop_next_prompt(
    queued_prompts: list[str],
) -> tuple[str | None, list[str]]:
    """Pop the next queued prompt; return ``(prompt, remaining)``.

    Returns ``(None, [])`` when no prompts are queued. The remaining
    list is a new list (not a view), safe to pass into the recursive
    round-advance call without aliasing.
    """
    if not queued_prompts:
        return None, []
    remaining = list(queued_prompts)
    next_prompt = remaining.pop(0)
    return next_prompt, remaining


def is_eligible_voter(voter_id: int, players: list[int]) -> bool:
    """Return ``True`` when ``voter_id`` is in the player pool.

    Tiny one-liner the cog uses to gate the vote-select callback
    behind the lobby pool — extracted so the rule lives in one place.
    """
    return voter_id in players
