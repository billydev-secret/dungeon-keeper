"""Tests for bot_modules.services.invite_tracker.

The old collector attributed ~15% of joins: single-use invites vanish from
guild.invites() before the diff ever sees their count rise, join bursts
swallowed all but the first join of a +N delta, and every failure was
silent. Each of those misses gets a test against the pure diff.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot_modules.services.invite_tracker import (
    InviteSnapshot,
    cache_invite_create,
    cache_invite_delete,
    diff_invite_snapshot,
    _invite_cache,
    snapshot_invites,
)

ALICE, BOB = 111, 222


def snap(uses, max_uses=0, inviter=ALICE):
    return InviteSnapshot(uses=uses, max_uses=max_uses, inviter_id=inviter)


@pytest.fixture(autouse=True)
def _clean_cache():
    _invite_cache.clear()
    yield
    _invite_cache.clear()


# ── diff: use-count path ─────────────────────────────────────────────


def test_diff_attributes_use_count_increase():
    inviter, code, updated = diff_invite_snapshot(
        {"aaa": snap(3)}, {"aaa": snap(4)}
    )
    assert (inviter, code) == (ALICE, "aaa")
    assert updated["aaa"].uses == 4


def test_diff_attributes_brand_new_code_first_use():
    inviter, code, _ = diff_invite_snapshot({}, {"fresh": snap(1, inviter=BOB)})
    assert (inviter, code) == (BOB, "fresh")


def test_diff_burst_drains_one_use_per_join():
    """+2 delta from two rapid joins: each diff consumes exactly one use.

    The old collector wrote the new count wholesale, so the second join's
    diff saw no change and was silently dropped.
    """
    old = {"aaa": snap(3)}
    new = {"aaa": snap(5)}  # two joins landed before the first diff ran

    inviter1, _, cache_after_first = diff_invite_snapshot(old, new)
    assert inviter1 == ALICE
    assert cache_after_first["aaa"].uses == 4  # one use left unconsumed

    inviter2, _, cache_after_second = diff_invite_snapshot(cache_after_first, new)
    assert inviter2 == ALICE  # second join still attributable
    assert cache_after_second["aaa"].uses == 5


def test_diff_no_change_returns_none():
    inviter, code, _ = diff_invite_snapshot({"aaa": snap(3)}, {"aaa": snap(3)})
    assert (inviter, code) == (None, None)


def test_diff_ignores_increase_without_inviter():
    inviter, _, _ = diff_invite_snapshot(
        {"aaa": snap(0, inviter=None)}, {"aaa": snap(1, inviter=None)}
    )
    assert inviter is None


# ── diff: vanished single-use path ───────────────────────────────────


def test_diff_attributes_consumed_single_use_invite():
    """A 1-use invite is deleted by Discord on use — its count never rises.

    This was the collector's biggest silent miss (personal invites are
    typically single-use).
    """
    inviter, code, updated = diff_invite_snapshot(
        {"once": snap(0, max_uses=1, inviter=BOB)}, {}
    )
    assert (inviter, code) == (BOB, "once")
    assert "once" not in updated


def test_diff_vanished_code_not_near_cap_is_mod_deletion():
    inviter, code, _ = diff_invite_snapshot(
        {"aaa": snap(1, max_uses=5)}, {}
    )
    assert (inviter, code) == (None, None)


def test_diff_vanished_unlimited_code_is_mod_deletion():
    inviter, _, _ = diff_invite_snapshot({"aaa": snap(7, max_uses=0)}, {})
    assert inviter is None


def test_diff_prefers_use_count_over_vanished_code():
    inviter, code, _ = diff_invite_snapshot(
        {"gone": snap(0, max_uses=1, inviter=BOB), "aaa": snap(3)},
        {"aaa": snap(4)},
    )
    assert (inviter, code) == (ALICE, "aaa")


# ── cache event handlers ─────────────────────────────────────────────


def _fake_invite(code, uses=0, max_uses=0, inviter_id=ALICE):
    inviter = SimpleNamespace(id=inviter_id) if inviter_id else None
    return SimpleNamespace(code=code, uses=uses, max_uses=max_uses, inviter=inviter)


def test_cache_invite_create_tracks_new_code():
    cache_invite_create(1, _fake_invite("new", inviter_id=BOB))
    assert _invite_cache[1]["new"] == InviteSnapshot(0, 0, BOB)


def test_cache_invite_delete_forgets_mod_deleted_code():
    _invite_cache[1] = {"aaa": snap(1, max_uses=5)}
    cache_invite_delete(1, "aaa")
    assert "aaa" not in _invite_cache[1]


def test_cache_invite_delete_keeps_freshly_consumed_code_for_join_diff():
    """The delete event can beat the member-join event for a used-up invite;
    the snapshot must survive so the join's vanished-code diff can claim it."""
    _invite_cache[1] = {"once": snap(0, max_uses=1, inviter=BOB)}
    cache_invite_delete(1, "once")
    assert _invite_cache[1]["once"] == snap(0, max_uses=1, inviter=BOB)


def test_snapshot_invites_maps_fields():
    out = snapshot_invites(
        [_fake_invite("a", uses=2, max_uses=5), _fake_invite("b", inviter_id=None)]
    )
    assert out == {
        "a": InviteSnapshot(2, 5, ALICE),
        "b": InviteSnapshot(0, 0, None),
    }
