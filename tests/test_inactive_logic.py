"""Tests for the pure inactive-sweep candidate selection.

The auto-sweep is a destructive mass role-strip, so its who-gets-swept decision
is isolated in ``select_sweep_candidates`` and pinned here: threshold, ordering,
exclusions, and the safety cap.
"""

from __future__ import annotations

import pytest

from bot_modules.inactive.logic import (
    PreviewMember,
    PreviewRole,
    SweepCandidate,
    build_sweep_preview,
    select_sweep_candidates,
    stale_inactive_channel_id,
)

DAY = 86400.0
NOW = 1_000_000_000.0


def _sweep(last_seen, *, threshold_days=30, exclude=None, cap=25):
    return select_sweep_candidates(
        last_seen=last_seen,
        now=NOW,
        threshold_seconds=threshold_days * DAY,
        exclude_ids=exclude or set(),
        cap=cap,
    )


def test_member_idle_past_threshold_is_selected():
    candidates, overflow = _sweep({1: NOW - 40 * DAY})
    assert [c.user_id for c in candidates] == [1]
    assert overflow == 0


def test_member_active_within_threshold_is_skipped():
    candidates, overflow = _sweep({1: NOW - 10 * DAY})
    assert candidates == []
    assert overflow == 0


def test_exactly_at_threshold_is_selected():
    # idle == threshold qualifies (>=).
    candidates, _ = _sweep({1: NOW - 30 * DAY})
    assert [c.user_id for c in candidates] == [1]


def test_excluded_ids_never_selected():
    candidates, _ = _sweep({1: NOW - 40 * DAY, 2: NOW - 40 * DAY}, exclude={1})
    assert [c.user_id for c in candidates] == [2]


def test_sorted_most_idle_first():
    candidates, _ = _sweep({1: NOW - 40 * DAY, 2: NOW - 90 * DAY, 3: NOW - 50 * DAY})
    assert [c.user_id for c in candidates] == [2, 3, 1]


def test_cap_truncates_and_reports_overflow():
    last_seen = {uid: NOW - (100 - uid) * DAY for uid in range(1, 6)}  # all idle
    candidates, overflow = _sweep(last_seen, cap=2)
    assert len(candidates) == 2
    assert overflow == 3
    # The two kept are the most idle (smallest uid here has largest idle).
    assert [c.user_id for c in candidates] == [1, 2]


def test_zero_threshold_selects_nothing():
    candidates, overflow = _sweep({1: NOW - 999 * DAY}, threshold_days=0)
    assert candidates == []
    assert overflow == 0


def test_zero_cap_selects_nothing():
    candidates, overflow = _sweep({1: NOW - 40 * DAY}, cap=0)
    assert candidates == []
    assert overflow == 0


def test_idle_seconds_is_computed():
    candidates, _ = _sweep({1: NOW - 40 * DAY})
    assert candidates[0].idle_seconds == 40 * DAY
    assert candidates[0].last_seen == NOW - 40 * DAY


def test_empty_input():
    assert _sweep({}) == ([], 0)


# ── Dashboard dry-run preview ────────────────────────────────────────
#
# The preview tells an admin what enabling the sweep would cost. Getting the
# role list wrong here is worse than showing nothing: it would promise roles
# that survive, or hide ones that don't.

DEFAULT_ROLE_ID = 0  # @everyone
INACTIVE_ROLE_ID = 555
BOT_TOP_POSITION = 10


def _candidate(user_id: int, *, idle_days: float = 40.0) -> SweepCandidate:
    return SweepCandidate(
        user_id=user_id,
        last_seen=NOW - idle_days * DAY,
        idle_seconds=idle_days * DAY,
    )


def _preview_member(user_id: int, roles) -> tuple[int, PreviewMember]:
    """``roles`` items are ``(role_id, name, managed[, position])``.

    Position defaults to 1 — below the bot — for the cases that aren't about
    hierarchy. Returns the ``(user_id, member)`` pair ``_preview`` keys on.
    """
    return user_id, PreviewMember(
        display_name=f"u{user_id}",
        roles=[PreviewRole(*(r if len(r) == 4 else (*r, 1))) for r in roles],
    )


def _preview(members, *, candidates=None, tracked=None, bot_position=BOT_TOP_POSITION):
    # `members` is a list of (user_id, PreviewMember) pairs.
    by_id = dict(members)
    return build_sweep_preview(
        candidates=candidates or [_candidate(uid) for uid in by_id],
        members=by_id,
        default_role_id=DEFAULT_ROLE_ID,
        inactive_role_id=INACTIVE_ROLE_ID,
        bot_top_role_position=bot_position,
        tracked_user_ids=tracked if tracked is not None else set(by_id),
    )


@pytest.mark.parametrize(
    ("roles", "expected_removed", "expected_kept"),
    [
        pytest.param(
            [(11, "Member", False), (12, "Artist", False)],
            ["Member", "Artist"],
            [],
            id="plain-roles-all-removed",
        ),
        pytest.param(
            [(11, "Member", False), (12, "Server Booster", True)],
            ["Member"],
            ["Server Booster"],
            id="managed-role-kept",
        ),
        pytest.param(
            [(DEFAULT_ROLE_ID, "@everyone", False), (11, "Member", False)],
            ["Member"],
            [],
            id="everyone-never-listed",
        ),
        pytest.param(
            [(INACTIVE_ROLE_ID, "Inactive", False), (11, "Member", False)],
            ["Member"],
            [],
            id="inactive-role-never-listed",
        ),
        pytest.param(
            [(DEFAULT_ROLE_ID, "@everyone", False), (12, "Twitch Sub", True)],
            [],
            ["Twitch Sub"],
            id="only-managed-roles-loses-nothing",
        ),
    ],
)
def test_preview_role_lists_mirror_the_strip(roles, expected_removed, expected_kept):
    sweepable, blocked = _preview([_preview_member(1, roles)])
    assert blocked == []
    assert [r.removed_role_names for r in sweepable] == [expected_removed]
    assert [r.kept_managed_role_names for r in sweepable] == [expected_kept]


def test_member_with_only_managed_roles_is_still_swept():
    # They lose no roles but still get moved and given @Inactive, so dropping
    # them from the listing would under-report the sweep.
    sweepable, _ = _preview([_preview_member(1, [(12, "Server Booster", True)])])
    assert [r.user_id for r in sweepable] == [1]
    assert sweepable[0].removed_role_names == []


def test_members_above_the_bot_are_reported_as_blocked():
    """The sweep would select these and then fail silently on Forbidden."""
    sweepable, blocked = _preview(
        [
            _preview_member(1, [(11, "Member", False, 1)]),
            _preview_member(2, [(12, "Staff", False, 99)]),
        ]
    )
    assert [r.user_id for r in sweepable] == [1]
    assert [r.user_id for r in blocked] == [2]


def test_member_level_with_the_bot_is_blocked():
    # Discord refuses role edits on an equal role position, not just a higher one.
    sweepable, blocked = _preview(
        [_preview_member(1, [(11, "Member", False, BOT_TOP_POSITION)])]
    )
    assert sweepable == []
    assert [r.user_id for r in blocked] == [1]


def test_managed_role_above_the_bot_does_not_block():
    """Only strippable roles can fail on hierarchy.

    A booster/Twitch role outranking the bot is never touched by the sweep and
    doesn't stop it removing the roles below, so judging by the member's *top*
    role would file a perfectly sweepable member under "would fail" — and drop
    them from the count of who a sweep actually moves.
    """
    sweepable, blocked = _preview(
        [
            _preview_member(
                1,
                [(12, "Server Booster", True, 99), (11, "Member", False, 1)],
            )
        ]
    )
    assert [r.user_id for r in sweepable] == [1]
    assert blocked == []
    assert sweepable[0].removed_role_names == ["Member"]


def test_highest_strippable_role_decides_the_split():
    # One low role is not enough — any role due to be stripped that outranks the
    # bot fails the whole call.
    sweepable, blocked = _preview(
        [_preview_member(1, [(11, "Member", False, 1), (12, "Staff", False, 99)])]
    )
    assert sweepable == []
    assert [r.user_id for r in blocked] == [1]


def test_member_with_nothing_to_strip_is_never_blocked():
    # No remove_roles call is made, so there is no hierarchy check to fail.
    sweepable, blocked = _preview(
        [_preview_member(1, [(12, "Server Booster", True, 99)])]
    )
    assert [r.user_id for r in sweepable] == [1]
    assert blocked == []


def test_untracked_members_are_flagged():
    """No message history means they were aged from their join date alone."""
    sweepable, _ = _preview(
        [
            _preview_member(1, [(11, "Member", False)]),
            _preview_member(2, [(11, "Member", False)]),
        ],
        tracked={1},
    )
    assert {r.user_id: r.has_tracked_messages for r in sweepable} == {1: True, 2: False}


def test_preview_preserves_most_idle_first_order():
    members = [_preview_member(uid, [(11, "Member", False)]) for uid in (1, 2, 3)]
    candidates = [
        _candidate(1, idle_days=40),
        _candidate(2, idle_days=90),
        _candidate(3, idle_days=50),
    ]
    # Ordering is the selector's job; the preview must not resort it.
    sweepable, _ = _preview(members, candidates=[candidates[1], candidates[2], candidates[0]])
    assert [r.user_id for r in sweepable] == [2, 3, 1]
    assert sweepable[0].idle_seconds == 90 * DAY


def test_candidate_who_left_the_guild_is_dropped():
    # Selection and render are separate round-trips; a member can vanish between.
    sweepable, blocked = _preview([], candidates=[_candidate(404)])
    assert sweepable == []
    assert blocked == []


# ── Stale inactive-channel decision (/inactive panel re-point) ───────


def test_stale_channel_returned_when_repointed():
    assert stale_inactive_channel_id("777", 888) == 777


def test_stale_channel_none_when_unchanged():
    assert stale_inactive_channel_id("888", 888) is None


def test_stale_channel_none_when_unset():
    assert stale_inactive_channel_id(None, 888) is None
    assert stale_inactive_channel_id("", 888) is None
    assert stale_inactive_channel_id("0", 888) is None


def test_stale_channel_none_when_garbage():
    assert stale_inactive_channel_id("not-an-id", 888) is None
