"""The /grant prerequisite gate.

The production shape: the Member grant requires the verification role, so a
newcomer who never verified can't be granted Member. The gate shipped
unreachable — ``/grant`` never passed ``required_role_id`` to the executor —
so these cases are the enforcement, not decoration.
"""

from __future__ import annotations

import pytest

from bot_modules.services.role_grant_logic import (
    GATE_MISSING_PREREQUISITE,
    GATE_NO_PERMISSION,
    GATE_OK,
    GATE_PREREQUISITE_DELETED,
    grant_refusal,
    prerequisite_gate,
    prerequisites_for_role,
)

# The production shape: Member is gated behind verification, NSFW is a grant
# with no prerequisite, and one grant is parked on "(none)" (role_id 0).
_GRANTS = {
    "denizen": {"role_id": 100, "required_role_id": 900},
    "nsfw": {"role_id": 200, "required_role_id": 0},
    "kink": {"role_id": 0, "required_role_id": 0},
}


@pytest.mark.parametrize(
    ("required_role_id", "exists", "held", "is_admin", "expected"),
    [
        # No prerequisite configured — the default for every grant.
        pytest.param(0, False, False, False, GATE_OK, id="unconfigured"),
        pytest.param(0, False, False, True, GATE_OK, id="unconfigured-admin"),
        # The bug: verification absent, grant attempted.
        pytest.param(
            555, True, False, False, GATE_MISSING_PREREQUISITE, id="missing"
        ),
        pytest.param(555, True, True, False, GATE_OK, id="held"),
        # Admins override; moderators do not (is_mod isn't an input at all).
        pytest.param(555, True, False, True, GATE_OK, id="admin-bypasses-missing"),
        # Fails closed when the required role was deleted...
        pytest.param(
            555, False, False, False, GATE_PREREQUISITE_DELETED, id="deleted"
        ),
        # ...except for the admin who has to go fix the config.
        pytest.param(555, False, False, True, GATE_OK, id="admin-bypasses-deleted"),
        # A negative id is as unconfigured as 0 — guards against a hand-edited
        # DB row or a picker that writes -1 for "(none)".
        pytest.param(-1, False, False, False, GATE_OK, id="negative-id"),
    ],
)
def test_prerequisite_gate(required_role_id, exists, held, is_admin, expected):
    assert (
        prerequisite_gate(
            required_role_id=required_role_id,
            required_role_exists=exists,
            target_has_required=held,
            actor_is_admin=is_admin,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("grants", "role_id", "expected"),
    [
        # The bypass this closes: a role menu published for the gated role.
        pytest.param(_GRANTS, 100, (900,), id="gated-grant-role"),
        pytest.param(_GRANTS, 200, (), id="grant-role-without-prerequisite"),
        pytest.param(_GRANTS, 300, (), id="role-that-is-not-a-grant"),
        # "(none)" stores role_id 0; a menu can't hold role 0, but nothing
        # should ever match on it either.
        pytest.param(_GRANTS, 0, (), id="unset-grant-never-matches"),
        pytest.param({}, 100, (), id="no-grants-configured"),
        # Two grants on one role: both prerequisites have to be cleared.
        pytest.param(
            {
                "a": {"role_id": 100, "required_role_id": 900},
                "b": {"role_id": 100, "required_role_id": 800},
                "c": {"role_id": 100, "required_role_id": 900},
            },
            100,
            (800, 900),
            id="two-grants-one-role-deduped",
        ),
    ],
)
def test_prerequisites_for_role(grants, role_id, expected):
    assert prerequisites_for_role(grants, role_id) == expected


def test_missing_prerequisite_implies_the_role_exists():
    """The caller dereferences the role to name it in the refusal, so
    MISSING_PREREQUISITE must never be returned for a role that's gone."""
    assert (
        prerequisite_gate(
            required_role_id=555,
            required_role_exists=False,
            target_has_required=False,
            actor_is_admin=False,
        )
        != GATE_MISSING_PREREQUISITE
    )


# ── which refusal /grant owes the caller (todo #176) ────────────────────────
#
# The reported bug: a greeter ran /grant on a newcomer who hadn't verified and
# got the bare "You don't have permission to use this command." The command
# checked the *actor's* allow-list before it ever looked at the member, so the
# one fact worth telling them — the member hasn't verified — was never reached.
# Billy's call: a refusal about the member outranks a refusal about the actor,
# accepting that anyone who can run /grant now learns a member's prerequisite
# state. These rows are that ordering; the strings live in the command.


@pytest.mark.parametrize(
    ("may_grant", "required_role_id", "exists", "held", "is_admin", "expected"),
    [
        # Nothing in the way.
        pytest.param(True, 0, False, False, False, GATE_OK, id="permitted-no-prereq"),
        pytest.param(True, 555, True, True, False, GATE_OK, id="permitted-prereq-held"),
        # Permitted actor, member not verified — unchanged by the reorder.
        pytest.param(
            True, 555, True, False, False, GATE_MISSING_PREREQUISITE,
            id="permitted-prereq-missing",
        ),
        # THE BUG. Not on the allow-list *and* the member hasn't verified: the
        # member's state is what the caller can act on, so it wins. Before the
        # fix this was GATE_NO_PERMISSION and the verification never surfaced.
        pytest.param(
            False, 555, True, False, False, GATE_MISSING_PREREQUISITE,
            id="unpermitted-prereq-missing-reports-the-member",
        ),
        # ...and a deleted prerequisite still outranks the actor gate, so a
        # broken gate is reported as broken rather than hidden behind it.
        pytest.param(
            False, 555, False, False, False, GATE_PREREQUISITE_DELETED,
            id="unpermitted-prereq-deleted-reports-the-config",
        ),
        # With nothing wrong on the member's side the actor gate is the honest
        # answer, so the allow-list keeps working exactly as before.
        pytest.param(
            False, 555, True, True, False, GATE_NO_PERMISSION,
            id="unpermitted-prereq-held",
        ),
        pytest.param(
            False, 0, False, False, False, GATE_NO_PERMISSION,
            id="unpermitted-no-prereq",
        ),
        # An admin passes both gates without being listed anywhere. Asserted on
        # the logic rather than inherited from can_use_grant_role's own admin
        # bypass, so the ordering can't quietly start depending on the caller.
        pytest.param(False, 555, True, False, True, GATE_OK, id="admin-bypasses-both"),
        pytest.param(False, 555, False, False, True, GATE_OK, id="admin-bypasses-deleted"),
    ],
)
def test_grant_refusal(may_grant, required_role_id, exists, held, is_admin, expected):
    assert (
        grant_refusal(
            actor_may_grant=may_grant,
            required_role_id=required_role_id,
            required_role_exists=exists,
            target_has_required=held,
            actor_is_admin=is_admin,
        )
        == expected
    )
