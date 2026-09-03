"""Community ballot rules: arithmetic, lifecycle, and the erasure guarantee.

The arithmetic is the part with teeth — simple majority, abstentions counting
for neither side, **ties failing**, and no minimum turnout — so every boundary
is a parametrised row rather than prose. The rest covers what a real ballot
runs into: a member changing their vote, one who leaves, one who loses sight of
the thread, a deadline sweep racing a moderator's Close press, and an erasure
landing on a ballot that has already been decided.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import policy_ballot_service as svc
from bot_modules.services.moderation import create_policy_ticket
from bot_modules.services.privacy_service import purge_user_data
from tests.db_template import migrated_db

GUILD = 10


@pytest.fixture
def conn(tmp_path):
    with open_db(migrated_db(tmp_path / "ballots.db")) as c:
        yield c


def _ticket(c, *, guild_id: int = GUILD, channel_id: int = 500) -> int:
    return create_policy_ticket(
        c,
        guild_id=guild_id,
        creator_id=1,
        channel_id=channel_id,
        title="Quiet hours",
        description="",
    )


def _ballot(c, *, guild_id: int = GUILD, closes_at: float = 2000.0, now: float = 1000.0):
    policy_id = _ticket(c, guild_id=guild_id)
    return svc.open_ballot(
        c,
        guild_id=guild_id,
        policy_id=policy_id,
        channel_id=500,
        question="Should we have quiet hours?",
        opened_by=7,
        closes_at=closes_at,
        now=now,
    )


# ── The arithmetic ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("yes", "no", "expected"),
    [
        pytest.param(3, 1, svc.OUTCOME_PASSED, id="clear-majority"),
        pytest.param(1, 0, svc.OUTCOME_PASSED, id="one-vote-carries"),
        pytest.param(11, 10, svc.OUTCOME_PASSED, id="one-over"),
        pytest.param(10, 10, svc.OUTCOME_FAILED, id="tie-fails"),
        pytest.param(10, 11, svc.OUTCOME_FAILED, id="one-under"),
        pytest.param(0, 1, svc.OUTCOME_FAILED, id="single-no"),
        pytest.param(0, 0, svc.OUTCOME_FAILED, id="nobody-voted-fails"),
    ],
)
def test_ballot_outcome_is_a_simple_majority_with_ties_failing(yes, no, expected):
    assert svc.ballot_outcome(yes, no) == expected


def test_abstentions_count_for_neither_side(conn):
    """20 abstentions cannot rescue a 1-2 loss, or block a 2-1 win."""
    ballot_id = _ballot(conn)
    for uid in range(100, 120):
        svc.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=GUILD, user_id=uid,
            choice="abstain", now=1100.0,
        )
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=1, choice="yes", now=1100.0
    )
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=2, choice="no", now=1100.0
    )
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=3, choice="no", now=1100.0
    )

    closed = svc.close_ballot(conn, ballot_id, closed_by=None, now=2000.0)

    assert closed is not None
    assert closed["outcome"] == svc.OUTCOME_FAILED
    assert svc.frozen_counts(closed) == (1, 2, 20)


def test_a_ballot_nobody_voted_in_still_resolves(conn):
    ballot_id = _ballot(conn)

    closed = svc.close_ballot(conn, ballot_id, closed_by=None, now=2000.0)

    assert closed is not None
    assert closed["outcome"] == svc.OUTCOME_FAILED
    assert svc.frozen_counts(closed) == (0, 0, 0)


# ── Casting, changing, refusing ───────────────────────────────────────


def test_a_vote_is_recorded_and_tallied(conn):
    ballot_id = _ballot(conn)

    assert svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=42, choice="yes", now=1100.0
    )

    assert svc.tally_ballot(conn, ballot_id) == {"yes": [42], "no": [], "abstain": []}


def test_pressing_twice_replaces_rather_than_stacks(conn):
    ballot_id = _ballot(conn)
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=42, choice="yes", now=1100.0
    )
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=42, choice="no", now=1200.0
    )

    assert svc.tally_ballot(conn, ballot_id) == {"yes": [], "no": [42], "abstain": []}
    assert len(svc.get_ballot_votes(conn, ballot_id)) == 1


def test_a_vote_on_a_closed_ballot_is_refused(conn):
    ballot_id = _ballot(conn)
    svc.close_ballot(conn, ballot_id, closed_by=9, now=1500.0)

    assert not svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=42, choice="yes", now=1600.0
    )
    assert svc.get_ballot_votes(conn, ballot_id) == []


def test_a_vote_on_a_ballot_that_does_not_exist_is_refused(conn):
    assert not svc.cast_ballot_vote(
        conn, ballot_id=999, guild_id=GUILD, user_id=42, choice="yes", now=1100.0
    )


@pytest.mark.parametrize("bad", ["", "maybe", "YES!", "1", "yes no"])
def test_a_choice_outside_the_vocabulary_is_rejected(conn, bad):
    ballot_id = _ballot(conn)

    with pytest.raises(ValueError):
        svc.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=GUILD, user_id=42, choice=bad,
            now=1100.0,
        )


@pytest.mark.parametrize(
    ("raw", "expected"), [("Yes", "yes"), (" NO ", "no"), ("Abstain", "abstain")]
)
def test_choices_are_normalised(raw, expected):
    assert svc.normalise_choice(raw) == expected


# ── Who may press ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("is_bot", "can_view", "closed", "expected"),
    [
        pytest.param(False, True, False, True, id="member-who-can-see-the-thread"),
        pytest.param(False, False, False, False, id="lost-sight-of-the-thread"),
        pytest.param(True, True, False, False, id="a-bot"),
        pytest.param(False, True, True, False, id="ballot-already-closed"),
    ],
)
def test_can_cast_gates_on_visibility_bothood_and_openness(
    conn, is_bot, can_view, closed, expected
):
    ballot_id = _ballot(conn)
    if closed:
        svc.close_ballot(conn, ballot_id, closed_by=9, now=1500.0)
    ballot = svc.get_ballot(conn, ballot_id)
    assert ballot is not None

    assert (
        svc.can_cast(ballot=ballot, is_bot=is_bot, can_view_thread=can_view) is expected
    )


def test_a_member_who_leaves_mid_ballot_keeps_their_cast_vote(conn):
    """Nothing re-checks membership at close: the electorate was 'whoever could
    see the thread when they pressed', and a departure does not retract a vote
    any more than it retracts a message they posted."""
    ballot_id = _ballot(conn)
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=42, choice="yes", now=1100.0
    )

    closed = svc.close_ballot(conn, ballot_id, closed_by=None, now=2000.0)

    assert closed is not None
    assert svc.frozen_counts(closed) == (1, 0, 0)


# ── Closing ───────────────────────────────────────────────────────────


def test_close_freezes_the_counts_onto_the_row(conn):
    ballot_id = _ballot(conn)
    for uid, choice in ((1, "yes"), (2, "yes"), (3, "no"), (4, "abstain")):
        svc.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=GUILD, user_id=uid, choice=choice,
            now=1100.0,
        )

    closed = svc.close_ballot(conn, ballot_id, closed_by=99, now=1500.0)

    assert closed is not None
    assert svc.frozen_counts(closed) == (2, 1, 1)
    assert closed["outcome"] == svc.OUTCOME_PASSED
    assert closed["closed_by"] == 99
    assert closed["closed_at"] == 1500.0
    assert not svc.is_open(closed)


def test_closing_twice_is_refused_so_a_sweep_cannot_beat_a_moderator(conn):
    """The deadline sweep and a Close press can fire together. Exactly one of
    them may write the result — a second close would re-freeze the counts under
    a result that had already been announced."""
    ballot_id = _ballot(conn)
    first = svc.close_ballot(conn, ballot_id, closed_by=99, now=1500.0)

    second = svc.close_ballot(conn, ballot_id, closed_by=None, now=2000.0)

    assert first is not None
    assert second is None
    row = svc.get_ballot(conn, ballot_id)
    assert row is not None
    assert row["closed_by"] == 99
    assert row["closed_at"] == 1500.0


def test_a_cancelled_ballot_keeps_its_counts_but_claims_no_result(conn):
    ballot_id = _ballot(conn)
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=1, choice="yes", now=1100.0
    )

    closed = svc.close_ballot(
        conn, ballot_id, closed_by=99, cancelled=True, now=1500.0
    )

    assert closed is not None
    assert closed["outcome"] == svc.OUTCOME_CANCELLED
    assert svc.frozen_counts(closed) == (1, 0, 0)


# ── The deadline sweep ────────────────────────────────────────────────


def test_find_expired_ballots_returns_only_past_deadline_open_ones(conn):
    past = _ballot(conn, closes_at=1500.0)
    future = _ballot(conn, closes_at=9000.0)
    already_closed = _ballot(conn, closes_at=1500.0)
    svc.close_ballot(conn, already_closed, closed_by=1, now=1400.0)

    expired = svc.find_expired_ballots(conn, GUILD, now=2000.0)

    assert [b["id"] for b in expired] == [past]
    assert future not in [b["id"] for b in expired]


def test_a_ballot_with_no_deadline_never_expires(conn):
    """`closes_at = 0` is the guild's voting-deadline dial set to 0 — auto
    resolution off, exactly as it is for the mod vote."""
    never = _ballot(conn, closes_at=0.0)

    assert svc.find_expired_ballots(conn, GUILD, now=10_000_000.0) == []
    ballot = svc.get_ballot(conn, never)
    assert ballot is not None
    assert not svc.is_expired(ballot, 10_000_000.0)


def test_the_sweep_is_scoped_to_one_guild(conn):
    mine = _ballot(conn, guild_id=GUILD, closes_at=1500.0)
    _ballot(conn, guild_id=99, closes_at=1500.0)

    assert [b["id"] for b in svc.find_expired_ballots(conn, GUILD, now=2000.0)] == [mine]


def test_is_expired_is_false_for_a_closed_ballot(conn):
    ballot_id = _ballot(conn, closes_at=1500.0)
    svc.close_ballot(conn, ballot_id, closed_by=1, now=1400.0)
    ballot = svc.get_ballot(conn, ballot_id)

    assert ballot is not None
    assert not svc.is_expired(ballot, 9999.0)


# ── Bookkeeping the Discord surface needs ─────────────────────────────


def test_the_thread_and_card_ids_are_attached_after_the_row_exists(conn):
    ballot_id = _ballot(conn)

    svc.attach_ballot_message(conn, ballot_id, thread_id=600, message_id=601)

    ballot = svc.get_ballot(conn, ballot_id)
    assert ballot is not None
    assert (ballot["thread_id"], ballot["message_id"]) == (600, 601)


def test_only_one_ballot_at_a_time_is_open_for_a_proposal(conn):
    policy_id = _ticket(conn)
    ballot_id = svc.open_ballot(
        conn, guild_id=GUILD, policy_id=policy_id, channel_id=500,
        question="q", opened_by=7, closes_at=2000.0, now=1000.0,
    )

    found = svc.get_open_ballot_for_policy(conn, policy_id)
    assert found is not None and found["id"] == ballot_id

    svc.close_ballot(conn, ballot_id, closed_by=1, now=1500.0)
    assert svc.get_open_ballot_for_policy(conn, policy_id) is None


def test_list_ballots_is_guild_scoped_and_newest_first(conn):
    older = _ballot(conn, now=1000.0)
    newer = _ballot(conn, now=5000.0)
    _ballot(conn, guild_id=99, now=6000.0)

    assert [b["id"] for b in svc.list_ballots(conn, GUILD)] == [newer, older]


# ── Erasure ───────────────────────────────────────────────────────────


def test_erasure_clears_a_members_votes_but_not_a_frozen_result(conn):
    """The whole reason the counts are frozen onto the ballot row: a member
    erased after a ballot closed must not be able to move a decision that was
    already announced in the thread."""
    ballot_id = _ballot(conn)
    for uid, choice in ((1, "yes"), (2, "yes"), (3, "no")):
        svc.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=GUILD, user_id=uid, choice=choice,
            now=1100.0,
        )
    closed = svc.close_ballot(conn, ballot_id, closed_by=99, now=1500.0)
    assert closed is not None
    assert closed["outcome"] == svc.OUTCOME_PASSED

    purge_user_data(conn, GUILD, 1)

    assert [v["user_id"] for v in svc.get_ballot_votes(conn, ballot_id)] == [2, 3]
    after = svc.get_ballot(conn, ballot_id)
    assert after is not None
    assert svc.frozen_counts(after) == (2, 1, 0)
    assert after["outcome"] == svc.OUTCOME_PASSED


def test_erasure_removes_a_vote_from_a_still_open_ballot(conn):
    """Correct, and deliberately not the same as the closed case: an erasure is
    an out-of-band operator act, and the member is no longer a participant."""
    ballot_id = _ballot(conn)
    svc.cast_ballot_vote(
        conn, ballot_id=ballot_id, guild_id=GUILD, user_id=1, choice="yes", now=1100.0
    )

    purge_user_data(conn, GUILD, 1)

    assert svc.tally_ballot(conn, ballot_id) == {"yes": [], "no": [], "abstain": []}


def test_erasure_is_guild_scoped(conn):
    """A member erased in one guild keeps their ballot vote in another."""
    mine = _ballot(conn, guild_id=GUILD)
    theirs = _ballot(conn, guild_id=99)
    for bid, gid in ((mine, GUILD), (theirs, 99)):
        svc.cast_ballot_vote(
            conn, ballot_id=bid, guild_id=gid, user_id=1, choice="yes", now=1100.0
        )

    purge_user_data(conn, GUILD, 1)

    assert svc.get_ballot_votes(conn, mine) == []
    assert [v["user_id"] for v in svc.get_ballot_votes(conn, theirs)] == [1]
