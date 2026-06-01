"""Pure decision logic for the Free For All cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its modal handler; the Discord glue (sending the message, persisting
via ``modify_payload``) stays in the cog.

The one real piece of logic FFA has is :func:`add_anon_reply` — every
anonymous reply gets a sequential numeric id and is stashed in the
payload's ``anon_replies`` dict alongside the submitting user id. The
returned reply count is what the cog feeds back into
:func:`build_ffa_embed` so the status-bar count updates after each
submission.
"""

from __future__ import annotations

from typing import Any


def add_anon_reply(payload: dict[str, Any], user_id: int, text: str) -> int:
    """Append an anonymous reply to ``payload`` and return the new count.

    Mutates ``payload`` in place: ensures ``anon_replies`` exists, then
    inserts the new reply under a fresh sequential id (``len + 1``) so
    every reply has a stable handle even though the public post is
    anonymous. Stores both the submitting ``user_id`` (for the audit
    log) and the raw ``text``.

    The returned int is the total reply count after the insert — the
    cog passes it to the embed builder so the status bar updates in one
    place without re-counting.
    """
    anon_replies: dict[str, dict[str, Any]] = payload.setdefault("anon_replies", {})
    anon_id = len(anon_replies) + 1
    anon_replies[str(anon_id)] = {
        "user_id": user_id,
        "text": text,
    }
    return len(anon_replies)
