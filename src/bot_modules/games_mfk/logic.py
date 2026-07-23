"""Pure decision logic for the Marry/Fornicate/Kiss cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from its
button callbacks and the close-and-assign step; the Discord glue
(sending the message, persisting via ``modify_payload``) stays in the
cog.

Three reusable pieces are extracted:

* :func:`toggle_participant` — the Join-the-Pool button spine. Adds or
  removes ``user_id`` from the lobby's participant list and returns
  ``"joined"`` / ``"left"`` so the cog echoes the action in an
  ephemeral reply.
* :func:`parse_labels` — validates the custom-category input from the
  slash-command's ``options:`` parameter and returns either the parsed
  list of exactly 3 labels or ``None`` (with a friendly error string)
  when validation fails.
* :func:`assign_targets` — the close-and-assign engine. Given the
  participant list and an injectable RNG, produce ``{player_id:
  [target_a, target_b, target_c]}`` where each player's three targets
  are drawn from the rest of the pool with no self-pairing.

``serialize_assignments`` is a tiny dict transform that the cog used to
inline; pulling it out makes the end-game payload handoff testable.
"""

from __future__ import annotations

import random
from typing import Any

DEFAULT_LABELS: list[str] = ["Marry", "Fornicate", "Kiss"]
TARGETS_PER_PLAYER = 3
MIN_PARTICIPANTS = 4

# Maximum participants allowed in a single lobby. The close-and-assign
# embed adds one field per participant and a Discord embed hard-caps at
# 25 fields, so a bigger pool would 400 the results message and leave
# the game row stuck in "joining". Cap the join so the embed always fits.
MAX_PARTICIPANTS = 25


def toggle_participant(
    payload: dict[str, Any],
    user_id: int,
    max_participants: int = MAX_PARTICIPANTS,
) -> str:
    """Toggle ``user_id`` in ``payload``'s participant list.

    Mutates ``payload`` in place: ensures ``participants`` exists, then
    adds or removes ``user_id`` depending on current membership.

    Returns ``"joined"`` or ``"left"`` so the caller can drop the verb
    straight into "You've ___ the pool." ephemeral confirmation. Returns
    ``"full"`` (without mutating) when a *new* joiner would push the pool
    past ``max_participants`` — the results embed can't hold more than 25
    fields, so an unbounded pool would 400 the assignments message.
    Leaving is always allowed, even from a full pool.
    """
    participants: list[int] = payload.setdefault("participants", [])
    if user_id in participants:
        participants.remove(user_id)
        return "left"
    if len(participants) >= max_participants:
        return "full"
    participants.append(user_id)
    return "joined"


def parse_labels(options: str | None) -> tuple[list[str] | None, str | None]:
    """Parse the slash command's ``options:`` parameter into 3 labels.

    The slash-command lets the host override the default ``Marry /
    Fornicate / Kiss`` labels with a comma-separated string like
    ``"Cruise, Wedding, Vacation"``.

    Returns ``(labels, None)`` on success — caller passes ``labels``
    to :func:`build_mfk_embed` and the view. Returns ``(None, None)``
    when ``options`` is empty/missing (caller falls back to
    :data:`DEFAULT_LABELS`). Returns ``(None, error_msg)`` when the
    input doesn't parse to exactly three non-empty entries — caller
    sends ``error_msg`` to the user as an ephemeral reply.
    """
    if not options:
        return (None, None)
    parts = [p.strip() for p in options.split(",") if p.strip()]
    if len(parts) != TARGETS_PER_PLAYER:
        return (
            None,
            f"Need exactly 3 comma-separated options (got {len(parts)}). "
            "Example: `Cruise, Wedding, Vacation`",
        )
    return (parts, None)


def assign_targets(
    participants: list[int],
    rng: random.Random | None = None,
) -> dict[int, list[int]]:
    """Assign each participant three random targets from the rest of the pool.

    Mirrors the cog's close-and-assign rule:

    1. For each player, build the list of all other participants
       (everyone except themselves — players never get assigned to
        marry/fornicate/kiss themselves).
    2. Sample ``TARGETS_PER_PLAYER`` (3) targets uniformly without
       replacement using ``rng.sample``.

    Raises ``ValueError`` when fewer than :data:`MIN_PARTICIPANTS` (4)
    participants are supplied — there aren't enough other players to
    fill every slot without self-pairing. The cog guards this before
    calling so users see a friendly error instead.

    ``rng`` is injected so tests can pin the assignment; defaults to
    the module ``random``.
    """
    if len(participants) < MIN_PARTICIPANTS:
        raise ValueError(
            f"Need at least {MIN_PARTICIPANTS} participants "
            f"(got {len(participants)})."
        )
    chooser = rng if rng is not None else random
    assignments: dict[int, list[int]] = {}
    for player_id in participants:
        others = [p for p in participants if p != player_id]
        assignments[player_id] = chooser.sample(others, TARGETS_PER_PLAYER)
    return assignments


def serialize_assignments(
    assignments: dict[int, list[int]]
) -> dict[str, list[int]]:
    """Stringify assignment keys for the persisted payload.

    Discord's payload-JSON convention uses string keys for user IDs.
    The cog writes this into ``end_game(payload=...)`` so the round
    output survives a bot restart.
    """
    return {str(player_id): targets for player_id, targets in assignments.items()}
