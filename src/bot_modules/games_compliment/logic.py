"""Pure decision logic for the Spin-the-Compliment cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from its
button callbacks; the Discord glue (sending the message, persisting via
``modify_payload``) stays in the cog.

Two reusable pieces are extracted:

* :func:`toggle_participant` — the "Join" button spine. Adds or
  removes ``user_id`` from the lobby's participant list and returns the
  human-readable action ("added to" / "removed from") that the cog
  echoes back in an ephemeral reply.
* :func:`generate_pairings` — a thin wrapper around the shared Sattolo
  derangement helper. It's exposed here so tests don't need to import
  the shared util directly, and so the cog has one place to swap the
  algorithm if the rules ever change.

``serialize_pairings`` and :func:`pairing_ids` are tiny dict
transformations the cog used to inline; pulling them out makes the
end-game payload handoff testable.
"""

from __future__ import annotations

from typing import Any

from bot_modules.games.utils.derangement import random_derangement


def toggle_participant(payload: dict[str, Any], user_id: int) -> str:
    """Toggle ``user_id`` in ``payload``'s participant list.

    Mutates ``payload`` in place: ensures ``participants`` exists, then
    adds or removes ``user_id`` depending on current membership.

    Returns ``"added to"`` or ``"removed from"`` so the caller can drop
    the verb straight into a "You've been ___ the pool." sentence.
    """
    participants: list[int] = payload.setdefault("participants", [])
    if user_id in participants:
        participants.remove(user_id)
        return "removed from"
    participants.append(user_id)
    return "added to"


def generate_pairings(participants: list[int]) -> dict[int, int]:
    """Return ``{giver_id: receiver_id}`` for the close-and-generate step.

    Thin wrapper around the shared Sattolo derangement helper. Kept here
    so the cog imports from its sibling module rather than reaching into
    ``games.utils`` directly, and so future variants (forbidden pairs,
    weighted matching) can swap in without touching the cog.

    Returns an empty dict when fewer than 2 participants are supplied —
    matching the shared helper's contract.
    """
    return random_derangement(participants)


def serialize_pairings(pairings: dict[int, int]) -> dict[str, int]:
    """Stringify pairing keys for the persisted payload.

    Discord's payload-JSON convention uses string keys for user IDs.
    The cog writes this into ``end_game(payload=...)`` so the round
    output survives a bot restart.
    """
    return {str(giver): receiver for giver, receiver in pairings.items()}


def pairing_ids(pairings: dict[int, int]) -> list[int]:
    """Return every user id that appears in ``pairings``.

    Used to build the public mentions list when the cog announces the
    pairings — every giver AND every receiver is pinged so each player
    sees their assignment land in their notification tray. Order is
    preserved (givers in iteration order, then their receiver) and
    duplicates are de-duped while preserving order.
    """
    seen: dict[int, None] = {}
    for giver, receiver in pairings.items():
        if giver not in seen:
            seen[giver] = None
        if receiver not in seen:
            seen[receiver] = None
    return list(seen.keys())
