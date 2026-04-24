"""Tier 1 unit tests: jail pure logic."""

import time

import pytest
from freezegun import freeze_time

from jail.logic import (
    eligible_voters,
    is_jail_expired,
    jail_duration_seconds,
    resolve_policy_vote,
    restore_roles,
    snapshot_roles,
    tally_votes,
)
from services.moderation import fmt_duration, parse_duration


# ── parse_duration ────────────────────────────────────────────────────

@pytest.mark.parametrize("s,expected", [
    ("30m", 1800),
    ("1h", 3600),
    ("2d", 172_800),
    ("1w", 604_800),
    ("1d12h", 129_600),
    ("2h30m", 9000),
    ("", None),
    ("abc", None),
])
def test_parse_duration(s, expected):
    assert parse_duration(s) == expected


# ── fmt_duration ──────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,expected", [
    (3600, "1h"),
    (7200, "2h"),
    (86400, "1d"),
    (90000, "1d 1h"),
    (129600, "1d 12h"),
    (604800, "1w"),
])
def test_fmt_duration(secs, expected):
    assert fmt_duration(secs) == expected


# ── snapshot_roles / restore_roles ────────────────────────────────────

def test_snapshot_roles_returns_copy():
    original = [1, 2, 3]
    snap = snapshot_roles(original)
    assert snap == original
    snap.append(99)
    assert 99 not in original


def test_restore_roles_filters_missing():
    stored = [1, 2, 3, 4]
    available = {1, 3}
    assert restore_roles(stored, available) == [1, 3]


def test_restore_roles_empty_available():
    assert restore_roles([1, 2], set()) == []


# ── is_jail_expired ───────────────────────────────────────────────────

@freeze_time("2026-04-23 12:00:00")
def test_jail_not_yet_expired():
    jail = {"created_at": 0.0, "expires_at": time.time() + 3600}
    assert not is_jail_expired(jail)


@freeze_time("2026-04-23 12:00:00")
def test_jail_exactly_expired():
    jail = {"created_at": 0.0, "expires_at": time.time()}
    assert is_jail_expired(jail)


def test_jail_no_expiry_never_expires():
    jail = {"created_at": 0.0, "expires_at": None}
    assert not is_jail_expired(jail)


@freeze_time("2026-04-23 12:00:00")
def test_jail_duration_seconds():
    now = time.time()
    jail = {"created_at": now - 3600}
    assert jail_duration_seconds(jail) == pytest.approx(3600, abs=1)


# ── eligible_voters ───────────────────────────────────────────────────

def _member(uid, is_bot=False, is_admin=False, role_ids=None):
    return {
        "user_id": uid,
        "is_bot": is_bot,
        "is_administrator": is_admin,
        "role_ids": role_ids or [],
    }


def test_eligible_voters_mod_role():
    members = [_member(1, role_ids=[5001]), _member(2, role_ids=[9999])]
    eligible = eligible_voters(members, mod_role_ids={5001}, admin_role_ids=set())
    assert 1 in eligible
    assert 2 not in eligible


def test_eligible_voters_admin_flag():
    members = [_member(1, is_admin=True), _member(2)]
    eligible = eligible_voters(members, mod_role_ids=set(), admin_role_ids=set())
    assert 1 in eligible
    assert 2 not in eligible


def test_eligible_voters_excludes_bots():
    members = [_member(1, is_bot=True, role_ids=[5001])]
    eligible = eligible_voters(members, mod_role_ids={5001}, admin_role_ids=set())
    assert 1 not in eligible


# ── tally_votes ───────────────────────────────────────────────────────

def test_tally_votes_basic():
    vote_map = {1: "yes", 2: "no", 3: "abstain"}
    eligible = {1, 2, 3, 4}
    tally = tally_votes(vote_map, eligible)
    assert 1 in tally["yes"]
    assert 2 in tally["no"]
    assert 3 in tally["abstain"]
    assert 4 in tally["awaiting"]


def test_tally_ignores_ineligible_votes():
    vote_map = {99: "yes"}  # 99 is not eligible
    eligible = {1, 2}
    tally = tally_votes(vote_map, eligible)
    assert tally["yes"] == []
    assert set(tally["awaiting"]) == {1, 2}


# ── resolve_policy_vote ───────────────────────────────────────────────

def test_resolve_vote_adopted():
    eligible = {1, 2}
    tally = {"yes": [1, 2], "no": [], "abstain": [], "awaiting": []}
    assert resolve_policy_vote(tally, eligible) == "adopted"


def test_resolve_vote_rejected_by_no():
    eligible = {1, 2}
    tally = {"yes": [1], "no": [2], "abstain": [], "awaiting": []}
    assert resolve_policy_vote(tally, eligible) == "rejected"


def test_resolve_vote_pending_missing_votes():
    eligible = {1, 2, 3}
    tally = {"yes": [1], "no": [], "abstain": [], "awaiting": [2, 3]}
    assert resolve_policy_vote(tally, eligible) == "pending"
