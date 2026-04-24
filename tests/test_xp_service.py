"""Tests for services/xp_service.should_grant_level_role."""

from __future__ import annotations

from typing import Any

import pytest

from services.xp_service import LevelRoleDecision, should_grant_level_role

THRESHOLD = 5
ROLE_ID = 777

_GRANT_OK: dict[str, Any] = dict(
    new_level=THRESHOLD,
    role_grant_level=THRESHOLD,
    level_role_id=ROLE_ID,
    role_exists=True,
    member_already_has_role=False,
)


# ── happy path ────────────────────────────────────────────────────────


def test_grants_when_all_conditions_met():
    assert should_grant_level_role(**_GRANT_OK) is LevelRoleDecision.GRANT


def test_grants_when_above_threshold():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": 10})
        is LevelRoleDecision.GRANT
    )


# ── SKIP_NOT_CONFIGURED ───────────────────────────────────────────────


def test_skip_not_configured_when_role_id_zero():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "level_role_id": 0})
        is LevelRoleDecision.SKIP_NOT_CONFIGURED
    )


def test_skip_not_configured_when_role_id_negative():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "level_role_id": -1})
        is LevelRoleDecision.SKIP_NOT_CONFIGURED
    )


# ── SKIP_BELOW_THRESHOLD ──────────────────────────────────────────────


def test_skip_below_threshold_when_level_lower():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": THRESHOLD - 1})
        is LevelRoleDecision.SKIP_BELOW_THRESHOLD
    )


def test_skip_below_threshold_at_level_zero():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": 0})
        is LevelRoleDecision.SKIP_BELOW_THRESHOLD
    )


def test_threshold_is_inclusive():
    # Exactly at threshold → grant (not below)
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": THRESHOLD})
        is LevelRoleDecision.GRANT
    )


# ── SKIP_ROLE_MISSING ─────────────────────────────────────────────────


def test_skip_role_missing_when_configured_but_absent():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "role_exists": False})
        is LevelRoleDecision.SKIP_ROLE_MISSING
    )


# ── SKIP_ALREADY_HAS ──────────────────────────────────────────────────


def test_skip_already_has_when_member_has_role():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "member_already_has_role": True})
        is LevelRoleDecision.SKIP_ALREADY_HAS
    )


# ── priority ordering ─────────────────────────────────────────────────


def test_not_configured_beats_below_threshold():
    # id=0 AND below threshold → id check wins
    result = should_grant_level_role(
        **{**_GRANT_OK, "level_role_id": 0, "new_level": 0}
    )
    assert result is LevelRoleDecision.SKIP_NOT_CONFIGURED


def test_below_threshold_beats_role_missing():
    # below threshold AND role missing → level check wins
    result = should_grant_level_role(
        **{**_GRANT_OK, "new_level": 0, "role_exists": False}
    )
    assert result is LevelRoleDecision.SKIP_BELOW_THRESHOLD


def test_role_missing_beats_already_has():
    # role missing AND already_has flag somehow true → role-missing wins
    # (in practice already_has cannot be true when role doesn't exist, but
    #  the ordering guard is important anyway)
    result = should_grant_level_role(
        **{**_GRANT_OK, "role_exists": False, "member_already_has_role": True}
    )
    assert result is LevelRoleDecision.SKIP_ROLE_MISSING


# ── all skip reasons surface distinctly ──────────────────────────────


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, LevelRoleDecision.GRANT),
        ({"level_role_id": 0}, LevelRoleDecision.SKIP_NOT_CONFIGURED),
        ({"new_level": THRESHOLD - 1}, LevelRoleDecision.SKIP_BELOW_THRESHOLD),
        ({"role_exists": False}, LevelRoleDecision.SKIP_ROLE_MISSING),
        ({"member_already_has_role": True}, LevelRoleDecision.SKIP_ALREADY_HAS),
    ],
)
def test_each_decision_reachable(overrides, expected):
    assert should_grant_level_role(**{**_GRANT_OK, **overrides}) is expected
