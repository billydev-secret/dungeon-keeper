"""Tests for services.moderation role snapshot / restore functions."""

from __future__ import annotations

from services.moderation import compute_roles_to_restore, compute_roles_to_snapshot

EVERYONE = 100
JAILED = 200
MOD = 5001
LEVEL_5 = 5002
BOOSTER = 5003


# ── compute_roles_to_snapshot ─────────────────────────────────────────


def test_snapshot_excludes_everyone_role():
    result = compute_roles_to_snapshot([EVERYONE, MOD], EVERYONE, JAILED)
    assert result == [MOD]


def test_snapshot_excludes_jailed_role():
    # Covers the case where the user is being re-jailed and already has @Jailed
    result = compute_roles_to_snapshot([MOD, JAILED, LEVEL_5], EVERYONE, JAILED)
    assert result == [MOD, LEVEL_5]


def test_snapshot_preserves_order():
    result = compute_roles_to_snapshot(
        [LEVEL_5, BOOSTER, MOD, EVERYONE], EVERYONE, JAILED
    )
    assert result == [LEVEL_5, BOOSTER, MOD]


def test_snapshot_empty_role_list():
    assert compute_roles_to_snapshot([], EVERYONE, JAILED) == []


def test_snapshot_only_everyone_returns_empty():
    # Fresh account with no roles beyond @everyone
    assert compute_roles_to_snapshot([EVERYONE], EVERYONE, JAILED) == []


def test_snapshot_preserves_all_roles_when_no_exclusions_match():
    # Neither @everyone nor @Jailed in the input list
    result = compute_roles_to_snapshot([MOD, LEVEL_5, BOOSTER], EVERYONE, JAILED)
    assert result == [MOD, LEVEL_5, BOOSTER]


def test_snapshot_same_everyone_and_jailed_id_still_filters():
    # Defensive: if someone passes the same ID for both, it's still excluded
    result = compute_roles_to_snapshot([MOD, EVERYONE], EVERYONE, EVERYONE)
    assert result == [MOD]


# ── compute_roles_to_restore ──────────────────────────────────────────


def test_restore_all_roles_still_exist():
    stored = [MOD, LEVEL_5, BOOSTER]
    available = {MOD, LEVEL_5, BOOSTER, 9999}
    restorable, missing = compute_roles_to_restore(stored, available)
    assert restorable == [MOD, LEVEL_5, BOOSTER]
    assert missing == []


def test_restore_some_roles_deleted():
    stored = [MOD, LEVEL_5, BOOSTER]
    available = {MOD, BOOSTER}  # LEVEL_5 was deleted
    restorable, missing = compute_roles_to_restore(stored, available)
    assert restorable == [MOD, BOOSTER]
    assert missing == [LEVEL_5]


def test_restore_all_roles_deleted():
    stored = [MOD, LEVEL_5]
    available: set[int] = set()
    restorable, missing = compute_roles_to_restore(stored, available)
    assert restorable == []
    assert missing == [MOD, LEVEL_5]


def test_restore_empty_stored_returns_empty():
    restorable, missing = compute_roles_to_restore([], {MOD, LEVEL_5})
    assert restorable == []
    assert missing == []


def test_restore_preserves_order_in_both_outputs():
    stored = [BOOSTER, MOD, LEVEL_5, 9999]
    available = {MOD, BOOSTER}
    restorable, missing = compute_roles_to_restore(stored, available)
    assert restorable == [BOOSTER, MOD]
    assert missing == [LEVEL_5, 9999]


def test_restore_duplicates_in_stored_list_preserved():
    # Defensive: if duplicates somehow got stored, restoration doesn't silently
    # dedupe — caller sees exactly what was asked.
    stored = [MOD, MOD]
    restorable, missing = compute_roles_to_restore(stored, {MOD})
    assert restorable == [MOD, MOD]
    assert missing == []


# ── round-trip invariant ──────────────────────────────────────────────


def test_snapshot_then_restore_round_trip():
    """The roles saved on jail should all come back on unjail if nothing
    changed in the guild. This is the core safety property."""
    original_roles = [MOD, LEVEL_5, BOOSTER, EVERYONE]
    guild_role_ids = {MOD, LEVEL_5, BOOSTER, EVERYONE, JAILED}

    snapshot = compute_roles_to_snapshot(original_roles, EVERYONE, JAILED)
    restorable, missing = compute_roles_to_restore(snapshot, guild_role_ids)

    assert missing == []
    # Roles returned exclude @everyone (implicit) and @Jailed (correct)
    assert set(restorable) == {MOD, LEVEL_5, BOOSTER}


def test_snapshot_then_restore_after_role_deletion():
    """If a role is deleted while the user is jailed, it appears in missing,
    and the remaining roles are still restored."""
    original_roles = [MOD, LEVEL_5, BOOSTER]
    # LEVEL_5 gets deleted during the jail
    guild_role_ids_after = {MOD, BOOSTER, EVERYONE, JAILED}

    snapshot = compute_roles_to_snapshot(original_roles, EVERYONE, JAILED)
    restorable, missing = compute_roles_to_restore(snapshot, guild_role_ids_after)

    assert set(restorable) == {MOD, BOOSTER}
    assert missing == [LEVEL_5]
