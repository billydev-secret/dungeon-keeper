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
    GATE_OK,
    GATE_PREREQUISITE_DELETED,
    prerequisite_gate,
)


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
