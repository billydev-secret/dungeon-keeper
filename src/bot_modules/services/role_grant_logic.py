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

from collections.abc import Mapping
from typing import Any

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

#: The actor isn't on this grant's allow-list. Ranked **below** both
#: prerequisite verdicts by :func:`grant_refusal` — see its docstring.
GATE_NO_PERMISSION = "no_permission"


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


def grant_refusal(
    *,
    actor_may_grant: bool,
    required_role_id: int,
    required_role_exists: bool,
    target_has_required: bool,
    actor_is_admin: bool,
) -> str:
    """Which refusal ``/grant`` owes the caller, or :data:`GATE_OK` to proceed.

    **A refusal about the member outranks a refusal about the actor.** The
    command used to answer in the opposite order — allow-list first — so a
    greeter running ``/grant`` on a newcomer who had not verified was told
    only "You don't have permission to use this command." That sentence is
    true and useless: it points at the greeter, while the thing anyone could
    act on is that the member still has to verify. The permission check never
    got far enough to know a prerequisite existed.

    The cost, accepted deliberately (Billy's call on todo #176): anyone who
    can invoke ``/grant`` now learns whether a member holds a grant's
    prerequisite, whether or not they may use that grant. The prerequisite in
    production is the verification role, which is visible in the member list
    anyway, so this widens who is *told* rather than who can find out.

    A deleted prerequisite outranks the actor gate for the same reason and one
    more: it is the only report that a safety gate has become unsatisfiable,
    and admins — the people who would fix it — bypass the gate and never see
    it. Hiding it behind the allow-list as well would leave it with almost no
    audience at all.

    ``actor_may_grant`` is the allow-list answer
    (:meth:`AppContext.can_use_grant_role`), which has its own admin bypass.
    ``actor_is_admin`` is still taken separately so the ordering here is
    self-contained rather than inheriting that bypass from the caller.
    """
    gate = prerequisite_gate(
        required_role_id=required_role_id,
        required_role_exists=required_role_exists,
        target_has_required=target_has_required,
        actor_is_admin=actor_is_admin,
    )
    if gate != GATE_OK:
        return gate
    if actor_is_admin or actor_may_grant:
        return GATE_OK
    return GATE_NO_PERMISSION


def prerequisites_for_role(
    grant_roles: Mapping[str, Mapping[str, Any]], role_id: int
) -> tuple[int, ...]:
    """Every *Role Required First* that guards handing out *role_id*.

    A self-service route to a role — a role-menu button, not ``/grant`` — has
    to consult this, or the prerequisite is a gate with an open door beside it:
    the menu path never looked at the grant config at all, so publishing a
    button for a gated role (the verification-gated Member role is the
    production case) handed it to anyone who clicked.

    Keyed by role id rather than grant name, because the role id is what a menu
    option holds. Two grants may point at the same role with different
    prerequisites; all of them come back (sorted, deduped) and the caller must
    clear every one — for an access gate the stricter reading is the safe one.
    Empty when the role isn't a grant role, or its grant has no prerequisite.
    """
    found = {
        int(cfg.get("required_role_id") or 0)
        for cfg in grant_roles.values()
        if int(cfg.get("role_id") or 0) == role_id
    }
    return tuple(sorted(rid for rid in found if rid > 0))
