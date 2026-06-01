"""Pure decision logic for LegitLibs Quiplash mode.

All functions here take and return plain Python values (no Discord, no
DB, no asyncio) so they're unit-testable. The mode orchestrator in
``modes/quiplash.py`` calls these from inside the closures it passes to
``modify_payload``.

Two flavors live here:

* **Payload constructors** like :func:`build_initial_payload`.
* **In-place mutators** like :func:`add_player`, :func:`claim_start`,
  :func:`store_submission`. They mutate ``payload`` in place and return
  a ``bool`` flag when the action is conditional on game state.

Reveal-phase shaping (:func:`collect_complete_submissions`,
:func:`shuffle_reveal_order`) is here so the cog stays a thin
orchestrator.

:func:`clamp_tier` lives in :mod:`.classic_logic` and is re-exported
here for callers that want a single import point.
"""

from __future__ import annotations

import random
from typing import Any

from .classic_logic import clamp_tier as clamp_tier  # re-export

__all__ = [
    "clamp_tier",
    "build_initial_payload",
    "add_player",
    "claim_start",
    "store_submission",
    "submitted_count",
    "get_prior_submission",
    "collect_complete_submissions",
    "shuffle_reveal_order",
]


# ── Initial payload ──────────────────────────────────────────────────


def build_initial_payload(
    host_id: int,
    tier: int,
    template: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``payload`` dict for a fresh Quiplash game in ``joining`` state."""
    return {
        "mode": "quiplash",
        "tier": tier,
        "template_id": template["template_id"],
        "template": {
            "title": template["title"],
            "body": template["body"],
            "blanks": template["blanks"],
        },
        "players": [host_id],
        "host_id": host_id,
        "state": "joining",
        "submissions": {},
    }


# ── Join-phase mutators ──────────────────────────────────────────────


def add_player(payload: dict[str, Any], uid: int) -> bool:
    """Add ``uid`` to ``payload['players']`` if not already there.

    Returns True if added, False if they were already in.
    """
    players = payload.setdefault("players", [])
    if uid in players:
        return False
    players.append(uid)
    return True


def claim_start(payload: dict[str, Any]) -> bool:
    """Transition state from ``joining`` to ``filling``.

    Returns True on a successful transition, False if the round had
    already started (another caller beat them to it).
    """
    if payload.get("state") != "joining":
        return False
    payload["state"] = "filling"
    return True


# ── Submission storage ──────────────────────────────────────────────


def store_submission(
    payload: dict[str, Any],
    uid: int | str,
    fills: dict[str, str],
    partial: bool,
) -> bool:
    """Write a player's submission (full or partial) to the payload.

    Only acts when ``payload['state'] == 'filling'``. Returns True on
    save, False when the fill phase has ended. Submissions are keyed by
    ``str(uid)`` to match production behavior (JSON keys are strings).
    """
    if payload.get("state") != "filling":
        return False
    submissions = payload.setdefault("submissions", {})
    submissions[str(uid)] = {"fills": fills, "partial": partial}
    return True


# ── Read helpers ─────────────────────────────────────────────────────


def submitted_count(
    payload: dict[str, Any],
    player_ids: list[int],
) -> int:
    """How many ``player_ids`` have a non-partial submission stored."""
    submissions = payload.get("submissions", {})
    return sum(
        1 for uid in player_ids
        if str(uid) in submissions
        and not submissions[str(uid)].get("partial", False)
    )


def get_prior_submission(
    payload: dict[str, Any],
    uid: int | str,
) -> tuple[dict[str, str], bool]:
    """Look up ``uid``'s prior submission for modal pre-fill.

    Returns ``(prior_fills, had_complete)``:
    * ``prior_fills`` is the {blank_id: value} map (empty when no prior
      submission). Always a dict — even when the stored submission shape
      is unexpected.
    * ``had_complete`` is True when the prior submission was non-partial
      (used to switch the "saved!" toast to "updated!").
    """
    submissions = payload.get("submissions", {})
    prior = submissions.get(str(uid)) or {}
    if not isinstance(prior, dict):
        return {}, False
    fills = prior.get("fills", {})
    if not isinstance(fills, dict):
        fills = {}
    had_complete = bool(prior) and not prior.get("partial", False)
    return fills, had_complete


# ── Reveal helpers ───────────────────────────────────────────────────


def collect_complete_submissions(
    submissions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Filter the submissions dict to entries whose ``partial`` is False.

    Returns a new dict — the input is not mutated.
    """
    return {
        uid: data for uid, data in submissions.items()
        if not data.get("partial", False)
    }


def shuffle_reveal_order(
    uids: list[str],
    rng: random.Random | None = None,
) -> list[str]:
    """Return a shuffled copy of ``uids`` for anonymous reveal order.

    ``rng`` is injected so tests can pin the order; defaults to the
    module-level :mod:`random` in production.
    """
    chooser = rng if rng is not None else random
    shuffled = list(uids)
    chooser.shuffle(shuffled)
    return shuffled
