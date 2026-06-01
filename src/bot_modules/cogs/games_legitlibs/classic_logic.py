"""Pure decision logic for LegitLibs Classic mode.

All functions here take and return plain Python values (no Discord, no
DB, no asyncio) so they're unit-testable. The mode orchestrator in
``modes/classic.py`` calls these from inside the closures it passes to
``modify_payload``.

Two flavors live here:

* **Payload constructors** like :func:`build_initial_payload` — return a
  fresh dict to seed ``create_game``.
* **In-place mutators** like :func:`add_player`, :func:`claim_start`,
  :func:`store_round1_fills`, :func:`init_rescue`, :func:`freeze_rescue`,
  :func:`store_rescue_fills`. They mutate the payload dict in place and
  return a ``bool`` indicating whether the action actually took effect
  (used by callers to send "saved" vs "phase ended" responses).

Round-robin distribution helpers (``assign_blanks_round_robin``,
``compute_unfilled``, ``assign_rescue``, ``players_done_count``,
``unique_contributors``) live in :mod:`.distribution` — this module
imports :func:`rescuers_done_count` from here.

Tier-cap clamping (:func:`clamp_tier`) is also here because it's pure
and identical in both modes.
"""

from __future__ import annotations

from typing import Any


# ── Tier cap clamp (shared by both modes) ────────────────────────────


def clamp_tier(requested: int, channel_max: int) -> tuple[int, bool]:
    """Clamp ``requested`` to ``channel_max``.

    Returns ``(effective_tier, was_clamped)``. ``was_clamped`` is True
    when the caller should warn the user that their request was lowered.
    """
    if requested > channel_max:
        return channel_max, True
    return requested, False


# ── Initial payload ──────────────────────────────────────────────────


def build_initial_payload(
    host_id: int,
    tier: int,
    template: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``payload`` dict for a fresh Classic game in ``joining`` state.

    ``template`` must have ``template_id``, ``title``, ``body``,
    ``blanks``. The host is auto-joined.
    """
    return {
        "mode": "classic",
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
        "assignments": {},
        "fills": {},
    }


# ── Join-phase mutators ──────────────────────────────────────────────


def add_player(payload: dict[str, Any], uid: int) -> bool:
    """Add ``uid`` to ``payload['players']`` if not already there.

    Returns True if the player was added, False if they were already in.
    Caller should check ``payload['state'] == 'joining'`` first.
    """
    players = payload.setdefault("players", [])
    if uid in players:
        return False
    players.append(uid)
    return True


def remove_player(payload: dict[str, Any], uid: int) -> bool:
    """Remove ``uid`` from ``payload['players']``.

    Returns True if removed, False if they weren't in the list.
    """
    players = payload.get("players", [])
    if uid not in players:
        return False
    players.remove(uid)
    return True


def claim_start(
    payload: dict[str, Any],
    assignments: dict[str, int],
) -> bool:
    """Transition the payload from ``joining`` to ``filling``.

    Writes ``assignments`` and flips ``state`` only if the current state
    is ``joining``. Returns True on a successful transition (the caller
    "claimed" the start), False if another caller beat them to it.
    """
    if payload.get("state") != "joining":
        return False
    payload["assignments"] = assignments
    payload["state"] = "filling"
    return True


# ── Round 1 fill mutator ─────────────────────────────────────────────


def store_round1_fills(
    payload: dict[str, Any],
    fills: dict[str, str],
    by_uid: int,
) -> bool:
    """Write a player's submitted fills to ``payload['fills']``.

    Only acts when ``payload['state'] == 'filling'``. Returns True when
    fills were saved, False when the phase has already ended (caller
    should tell the user their fills were dropped).
    """
    if payload.get("state") != "filling":
        return False
    blank_fills = payload.setdefault("fills", {})
    for bid, val in fills.items():
        blank_fills[bid] = {"value": val, "by": by_uid}
    return True


# ── Rescue phase mutators ────────────────────────────────────────────


def init_rescue(
    payload: dict[str, Any],
    claim_deadline_ts: int,
) -> None:
    """Set up the rescue-claim phase on the payload.

    Mutates in place; always succeeds. The caller controls when to
    invoke (typically after the round-1 fill phase ends with leftovers).
    """
    payload["state"] = "rescuing_claim"
    payload["rescue"] = {
        "volunteers": [],
        "assignments": {},
        "claim_deadline": claim_deadline_ts,
        "fill_deadline": 0,
    }


def add_volunteer(payload: dict[str, Any], uid: int) -> str:
    """Add ``uid`` to the rescue volunteers list.

    Returns one of:
    * ``"closed"``    — rescue claim window is no longer open
    * ``"not_player"`` — user isn't in this round
    * ``"already"``    — user already volunteered
    * ``"added"``      — user added to the volunteer list
    """
    if payload.get("state") != "rescuing_claim":
        return "closed"
    if uid not in payload.get("players", []):
        return "not_player"
    rescue = payload.setdefault("rescue", {})
    vols = rescue.setdefault("volunteers", [])
    if uid in vols:
        return "already"
    vols.append(uid)
    return "added"


def freeze_rescue(
    payload: dict[str, Any],
    assignments: dict[str, int],
    fill_deadline_ts: int,
) -> None:
    """Lock in rescue assignments and set the rescue-fill deadline.

    Mutates in place. Used after the claim window closes and before the
    rescue-fill phase begins.
    """
    rescue = payload.setdefault("rescue", {})
    rescue["assignments"] = assignments
    rescue["fill_deadline"] = fill_deadline_ts


def set_rescue_fill_state(payload: dict[str, Any]) -> None:
    """Flip the payload state to ``rescuing_fill``. No-op safe to call twice."""
    payload["state"] = "rescuing_fill"


def store_rescue_fills(
    payload: dict[str, Any],
    fills: dict[str, str],
    by_uid: int,
) -> bool:
    """Write rescue-phase fills to ``payload['fills']``.

    Only acts when ``payload['state'] == 'rescuing_fill'``. Returns True
    on save, False when the phase has ended.
    """
    if payload.get("state") != "rescuing_fill":
        return False
    blank_fills = payload.setdefault("fills", {})
    for bid, val in fills.items():
        blank_fills[bid] = {"value": val, "by": by_uid}
    return True


# ── Read-only helpers ────────────────────────────────────────────────


def rescuers_done_count(
    rescue_assignments: dict[str, int],
    fills: dict[str, Any],
    rescuers: list[int],
) -> int:
    """How many rescuers have all their assigned rescue blanks filled.

    A rescuer with no assigned blanks does *not* count as done (unlike
    the round-1 :func:`distribution.players_done_count` semantics, which
    counts zero-assignment players as done — round-1 distributes across
    all joiners, so zero-assignment is a real outcome; rescue only ever
    lists volunteers who got assignments).
    """
    done = 0
    for pid in rescuers:
        their = [bid for bid, v in rescue_assignments.items() if v == pid]
        if their and all(bid in fills for bid in their):
            done += 1
    return done


def filter_rescuers(
    rescue_assignments: dict[str, int],
    volunteers: list[int],
) -> list[int]:
    """Volunteers who actually received rescue blanks.

    With more volunteers than unfilled blanks, the round-robin
    :func:`distribution.assign_rescue` only hands work to the first
    ``len(unfilled)`` volunteers; the rest just hover.
    """
    assigned = set(rescue_assignments.values())
    return [v for v in volunteers if v in assigned]


def my_blank_ids(
    assignments: dict[str, int],
    uid: int,
) -> list[str]:
    """Blanks assigned to ``uid`` in template order (dict iteration order).

    Used by both round-1 and rescue submit handlers to figure out which
    blanks to pre-fill in the modal.
    """
    return [bid for bid, pid in assignments.items() if pid == uid]


def existing_fill_values(
    blanks: list[dict[str, Any]],
    fills: dict[str, Any],
    blank_ids: list[str],
) -> dict[str, str]:
    """Build a {blank_id: prior_value} map for pre-filling the modal.

    ``blanks`` is the template-order blank list (used to filter to the
    caller's assigned ids); ``fills`` is the payload's fills dict
    (each value has shape ``{"value": str, "by": uid}``).
    """
    my_blanks = [b for b in blanks if b["id"] in blank_ids]
    return {
        b["id"]: fills[b["id"]]["value"]
        for b in my_blanks
        if b["id"] in fills
    }
