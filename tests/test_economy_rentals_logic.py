"""Tests for economy/rentals.py — the pure billing-decision logic.

``classify`` gets an exhaustive table drive (state × due/not-due × grace-age ×
cancel-flag × suspended). Note there is no ENTER_GRACE row: ``classify`` never
returns it — a due active rental returns CHARGE and the service downgrades to
grace only on an actual debit failure (see the module docstring).
"""

from __future__ import annotations

import pytest

from bot_modules.economy.rentals import (
    GRACE_SECONDS,
    WEEK_SECONDS,
    BillingAction,
    classify,
    effective_color_mode,
    entitled_perks,
)

NOW = 1_000_000.0
A = BillingAction


# ── classify: exhaustive matrix ────────────────────────────────────────

# (state, next_bill_at, grace_since, cancel, suspended) -> expected
_CASES = [
    # active, not due → NONE (with/without cancel flag, cancel is period-END)
    ("active", NOW + 10, None, False, False, A.NONE),
    ("active", NOW + 10, None, True, False, A.NONE),
    # active, exactly due → CHARGE (>= boundary counts as due)
    ("active", NOW, None, False, False, A.CHARGE),
    # active, past due → CHARGE
    ("active", NOW - 1, None, False, False, A.CHARGE),
    ("active", NOW - WEEK_SECONDS, None, False, False, A.CHARGE),
    # active, due + cancel flag → CANCEL_PERIOD_END (no charge)
    ("active", NOW, None, True, False, A.CANCEL_PERIOD_END),
    ("active", NOW - 1, None, True, False, A.CANCEL_PERIOD_END),
    # suspended always → NONE, regardless of due/cancel/state
    ("active", NOW - 1, None, False, True, A.NONE),
    ("active", NOW - 1, None, True, True, A.NONE),
    ("grace", NOW - 1, NOW - GRACE_SECONDS - 5, False, True, A.NONE),
    # grace, within window → RETRY (age just under 36h, and fresh)
    ("grace", NOW, NOW - 5, False, False, A.RETRY),
    ("grace", NOW, NOW - (GRACE_SECONDS - 1), False, False, A.RETRY),
    # grace, exactly at 36h → REVOKE (boundary: >= GRACE_SECONDS revokes)
    ("grace", NOW, NOW - GRACE_SECONDS, False, False, A.REVOKE),
    # grace, past window → REVOKE
    ("grace", NOW, NOW - GRACE_SECONDS - 100, False, False, A.REVOKE),
    # grace with cancel flag set still ages out normally (cancel is a no-op here)
    ("grace", NOW, NOW - 5, True, False, A.RETRY),
    ("grace", NOW, NOW - GRACE_SECONDS, True, False, A.REVOKE),
    # grace with a missing anchor → REVOKE defensively
    ("grace", NOW, None, False, False, A.REVOKE),
    # terminal states → NONE
    ("lapsed", NOW - 1, None, False, False, A.NONE),
    ("cancelled", NOW - 1, None, True, False, A.NONE),
]


@pytest.mark.parametrize(
    ("state", "next_bill_at", "grace_since", "cancel", "suspended", "expected"), _CASES
)
def test_classify_matrix(state, next_bill_at, grace_since, cancel, suspended, expected):
    assert (
        classify(state, next_bill_at, grace_since, cancel, suspended, NOW) is expected
    )


def test_classify_never_returns_enter_grace():
    # ENTER_GRACE is an outcome the service reports, never a classify decision.
    seen = {classify(c[0], c[1], c[2], c[3], c[4], NOW) for c in _CASES}
    assert BillingAction.ENTER_GRACE not in seen


# ── entitled_perks ─────────────────────────────────────────────────────


def _r(perk, state):
    return {"perk": perk, "state": state}


def test_entitled_perks_active_and_grace_grant():
    rows = [
        _r("role_color", "active"),
        _r("role_icon", "grace"),
        _r("role_name", "lapsed"),
        _r("role_gradient", "cancelled"),
    ]
    assert entitled_perks(rows) == {"role_color", "role_icon"}


def test_entitled_perks_empty():
    assert entitled_perks([]) == set()
    assert entitled_perks([_r("role_color", "lapsed")]) == set()


# ── effective_color_mode ───────────────────────────────────────────────


def test_color_mode_none():
    assert effective_color_mode(set()) == "none"
    assert effective_color_mode({"role_name", "role_icon"}) == "none"


def test_color_mode_solid_from_role_color():
    assert effective_color_mode({"role_color"}) == "solid"


def test_color_mode_gift_color_kind_retired():
    # A gifted color is a role_color rental since migration 091 — the old
    # gift_color kind must no longer grant anything on its own.
    assert effective_color_mode({"gift_color"}) == "none"


def test_color_mode_gradient_supersedes_solid():
    assert effective_color_mode({"role_gradient", "role_color"}) == "gradient"
    assert effective_color_mode({"role_gradient"}) == "gradient"


def test_color_mode_holographic_tops_everything():
    assert effective_color_mode({"role_holographic"}) == "holographic"
    # Holographic beats gradient and solid when a member holds several.
    assert (
        effective_color_mode({"role_holographic", "role_gradient", "role_color"})
        == "holographic"
    )
