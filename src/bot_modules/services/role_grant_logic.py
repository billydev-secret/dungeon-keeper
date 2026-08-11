"""Pure decision logic for ``/grant``'s prerequisite-role gate.

A grant can name a ``required_role_id`` — "a member can't receive this role
until they already hold that one" (the dashboard calls it *Role Required
First*). The production use is the verification gate: the Member grant
requires the verification role, so an unverified newcomer can't be made a
member.

The knob has been storable since migration 021 and settable on the dashboard,
but ``/grant`` never passed it to the executor, so the gate never ran and
members were granted roles whose prerequisite they did not hold. Splitting
the decision out here means the gate is exercised as a table rather than
through Discord mocks — CLAUDE.md's rule that a passing test *is* the
enforcement a safety gate demands.

Deliberately free of ``discord`` imports: callers reduce Discord state to
primitives and render the refusal themselves.
"""

from __future__ import annotations

#: The grant may proceed — no prerequisite, or it's satisfied, or an admin
#: is overriding.
GATE_OK = "ok"

#: The member doesn't hold the prerequisite. The refusal names it so the
#: greeter knows what has to happen first.
GATE_MISSING_PREREQUISITE = "missing_prerequisite"

#: A prerequisite is configured but the role is gone from the guild. Fails
#: **closed**: an unsatisfiable requirement blocks rather than waving
#: everyone through, because the alternative silently disables a safety gate
#: the moment someone deletes a role.
GATE_PREREQUISITE_DELETED = "prerequisite_deleted"


def prerequisite_gate(
    *,
    required_role_id: int,
    required_role_exists: bool,
    target_has_required: bool,
    actor_is_admin: bool,
) -> str:
    """Decide whether a grant clears its prerequisite.

    ``actor_is_admin`` is the only bypass, matching
    :meth:`AppContext.can_use_grant_role` — deliberately *not* ``is_mod``.
    Moderators are the people most likely to run ``/grant`` on a fresh
    arrival, so exempting them would leave the gate barely load-bearing;
    administrators keep the override so a guild can't wedge itself behind a
    prerequisite it can no longer satisfy. An admin bypasses the deleted-role
    refusal too — they're the ones who'd have to fix the config anyway.

    Returns one of :data:`GATE_OK`, :data:`GATE_MISSING_PREREQUISITE`, or
    :data:`GATE_PREREQUISITE_DELETED`.
    """
    if required_role_id <= 0:
        return GATE_OK
    if actor_is_admin:
        return GATE_OK
    if not required_role_exists:
        return GATE_PREREQUISITE_DELETED
    if not target_has_required:
        return GATE_MISSING_PREREQUISITE
    return GATE_OK
