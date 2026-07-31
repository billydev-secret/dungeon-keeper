"""No-contact list — pure decision logic.

Every test here stands in for a safety guarantee, not a formatting detail.
The two that matter most are the removal-authorisation block (who can lift a
protection) and the disclosure block (who is allowed to learn an entry
exists) — a regression in either hands a harasser something real.
"""

from __future__ import annotations

import pytest

from dataclasses import dataclass

from bot_modules.services.no_contact_logic import (
    KIND_MENTION,
    KIND_REPLY,
    REMOVAL_DENIED_MISSING,
    REMOVAL_DENIED_MUTUAL,
    SURFACE_WHISPER,
    alert_ping_prefix,
    alerts_for_message,
    can_remove,
    candidate_members_for,
    guess_round_blocked_for,
    is_self_pair,
    is_visible_to,
    pair_key,
    removal_denied_message,
    resolve_protected_user,
    surface_label,
)

ALICE = 100
BOB = 200
CAROL = 300


@dataclass(frozen=True)
class _Member:
    """Stand-in for the ``discord.Member`` the picker filter really receives."""

    id: int
MOD = 999


# ── Pair identity ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [(ALICE, BOB), (BOB, ALICE)],
)
def test_pair_key_is_order_independent(a, b):
    assert pair_key(a, b) == (ALICE, BOB)


def test_is_self_pair():
    assert is_self_pair(ALICE, ALICE)
    assert not is_self_pair(ALICE, BOB)


# ── Removal authorisation ────────────────────────────────────────────────


def test_protected_member_can_remove_their_own_entry():
    assert can_remove(protected_user_id=ALICE, actor_id=ALICE, actor_is_mod=False)


def test_other_party_cannot_remove():
    """The case the removal rule exists for.

    Bob must not be able to lift Alice's protection — not by pressuring her
    into it, and not by adding an entry himself and then removing it.
    """
    assert not can_remove(
        protected_user_id=ALICE, actor_id=BOB, actor_is_mod=False
    )


def test_mutual_entry_cannot_be_lifted_by_either_party():
    for actor in (ALICE, BOB):
        assert not can_remove(
            protected_user_id=None, actor_id=actor, actor_is_mod=False
        )


def test_mod_can_remove_anything():
    for protected in (ALICE, BOB, None):
        assert can_remove(
            protected_user_id=protected, actor_id=MOD, actor_is_mod=True
        )


@pytest.mark.parametrize(
    ("protect", "expected"),
    [
        (ALICE, ALICE),
        (BOB, BOB),
        (None, None),
        (CAROL, None),  # a third party can never hold removal rights
        (0, None),
    ],
)
def test_resolve_protected_user(protect, expected):
    assert resolve_protected_user(user_a=ALICE, user_b=BOB, protect=protect) == expected


# ── Disclosure ───────────────────────────────────────────────────────────


def test_entry_protecting_the_other_party_is_invisible():
    """Bob must not be able to learn Alice added an entry against him.

    This is the same disclosure the fake-success rule prevents, arriving
    through a much calmer door: him running a list command.
    """
    assert not is_visible_to(protected_user_id=ALICE, viewer_id=BOB)


def test_own_entry_is_visible():
    assert is_visible_to(protected_user_id=ALICE, viewer_id=ALICE)


def test_mutual_entry_is_visible_to_both():
    for viewer in (ALICE, BOB):
        assert is_visible_to(protected_user_id=None, viewer_id=viewer)


def test_removal_refusal_does_not_confirm_a_hidden_entry():
    """"You can't remove this" would confirm there is something to remove."""
    assert removal_denied_message(ALICE, actor_id=BOB) == REMOVAL_DENIED_MISSING


def test_removal_refusal_is_explicit_when_the_entry_is_visible():
    """A refusal on a visible entry can only mean a mutual separation.

    The other visible case — the protected member acting on their own entry —
    is allowed by ``can_remove``, so it never reaches the refusal path.
    """
    assert removal_denied_message(None, actor_id=ALICE) == REMOVAL_DENIED_MUTUAL
    assert removal_denied_message(None, actor_id=BOB) == REMOVAL_DENIED_MUTUAL


# ── Mention / reply alerting ─────────────────────────────────────────────


def test_mention_of_a_partner_alerts():
    alerts = alerts_for_message(
        author_id=BOB,
        mentioned_ids=[ALICE],
        reply_to_author_id=None,
        no_contact_partners=[ALICE],
    )
    assert [(a.actor_id, a.target_id, a.kind) for a in alerts] == [
        (BOB, ALICE, KIND_MENTION)
    ]


def test_reply_with_no_ping_still_alerts():
    """The hole a mention-only alert would leave.

    A Discord reply with the ping switched off never reaches
    ``message_mentions``, but it lands in front of her all the same.
    """
    alerts = alerts_for_message(
        author_id=BOB,
        mentioned_ids=[],
        reply_to_author_id=ALICE,
        no_contact_partners=[ALICE],
    )
    assert [(a.target_id, a.kind) for a in alerts] == [(ALICE, KIND_REPLY)]


def test_mention_and_reply_to_same_person_alerts_once():
    alerts = alerts_for_message(
        author_id=BOB,
        mentioned_ids=[ALICE],
        reply_to_author_id=ALICE,
        no_contact_partners=[ALICE],
    )
    assert len(alerts) == 1
    assert alerts[0].kind == KIND_MENTION


def test_mentioning_a_non_partner_does_not_alert():
    assert (
        alerts_for_message(
            author_id=BOB,
            mentioned_ids=[CAROL],
            reply_to_author_id=None,
            no_contact_partners=[ALICE],
        )
        == []
    )


def test_no_partners_short_circuits():
    assert (
        alerts_for_message(
            author_id=BOB,
            mentioned_ids=[ALICE, CAROL],
            reply_to_author_id=ALICE,
            no_contact_partners=[],
        )
        == []
    )


def test_self_mention_never_alerts():
    assert (
        alerts_for_message(
            author_id=BOB,
            mentioned_ids=[BOB],
            reply_to_author_id=BOB,
            no_contact_partners=[BOB],
        )
        == []
    )


def test_duplicate_mentions_collapse():
    alerts = alerts_for_message(
        author_id=BOB,
        mentioned_ids=[ALICE, ALICE, ALICE],
        reply_to_author_id=None,
        no_contact_partners=[ALICE],
    )
    assert len(alerts) == 1


def test_multiple_partners_each_alert():
    alerts = alerts_for_message(
        author_id=BOB,
        mentioned_ids=[ALICE, CAROL],
        reply_to_author_id=None,
        no_contact_partners=[ALICE, CAROL],
    )
    assert {a.target_id for a in alerts} == {ALICE, CAROL}


# ── Alert formatting ─────────────────────────────────────────────────────


def test_alert_ping_prefix():
    assert alert_ping_prefix(555) == "<@&555> "
    assert alert_ping_prefix(0) == ""


def test_surface_label_falls_back_to_raw_value():
    assert surface_label(SURFACE_WHISPER) == "Whisper"
    assert surface_label("something_new") == "something_new"
    assert surface_label("") == "Unknown"


# ── Guess Who ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("submitter", "answer", "blocked"),
    [
        (ALICE, ALICE, True),   # the ordinary case: answer is the submitter
        (CAROL, ALICE, True),   # her likeness, someone else posted it
        (ALICE, CAROL, True),   # she posted someone else's
        (CAROL, CAROL, False),  # nothing to do with her
    ],
)
def test_guess_round_blocked_covers_both_roles(submitter, answer, blocked):
    assert (
        guess_round_blocked_for(
            viewer_id=BOB,
            submitter_id=submitter,
            answer_id=answer,
            partners=[ALICE],
        )
        is blocked
    )


def test_candidate_picker_drops_partners():
    """Filtering her out of his picker is what makes silent-discard safe.

    If he cannot select her, he can never guess her correctly, so he can
    never notice a correct guess going unrecorded.
    """
    members = [_Member(ALICE), _Member(BOB), _Member(CAROL)]
    assert candidate_members_for(members, [ALICE]) == [_Member(BOB), _Member(CAROL)]


def test_candidate_picker_untouched_without_partners():
    members = [_Member(ALICE), _Member(BOB)]
    assert candidate_members_for(members, []) == members
