"""Tier 2 component tests: jail/policy View/Button/Modal structural assertions."""

from __future__ import annotations

import pytest

from bot_modules.commands.jail_commands import (
    PolicyBallotAbstainButton,
    PolicyBallotCloseButton,
    PolicyBallotNoButton,
    PolicyBallotYesButton,
    PolicyVoteAbstainButton,
    PolicyVoteNoButton,
    PolicyVoteYesButton,
    _JailModal,
)


# ── Policy vote button custom_ids ─────────────────────────────────────

def test_yes_button_custom_id_contains_policy_id():
    btn = PolicyVoteYesButton(10)
    assert btn.item.custom_id == "policy_vote:yes:10"


def test_no_button_custom_id_contains_policy_id():
    btn = PolicyVoteNoButton(10)
    assert btn.item.custom_id == "policy_vote:no:10"


def test_abstain_button_custom_id_contains_policy_id():
    btn = PolicyVoteAbstainButton(10)
    assert btn.item.custom_id == "policy_vote:abstain:10"


def test_vote_button_ids_are_deterministic():
    assert PolicyVoteYesButton(5).item.custom_id == PolicyVoteYesButton(5).item.custom_id
    assert PolicyVoteNoButton(5).item.custom_id == PolicyVoteNoButton(5).item.custom_id


# ── Vote button label lengths ─────────────────────────────────────────

_VOTE_BUTTONS = [
    PolicyVoteYesButton(1),
    PolicyVoteNoButton(1),
    PolicyVoteAbstainButton(1),
]


@pytest.mark.parametrize("btn", _VOTE_BUTTONS, ids=lambda b: b.item.label or "")
def test_vote_button_label_within_discord_limit(btn):
    assert len(btn.item.label or "") <= 80


# ── DynamicItem template patterns ─────────────────────────────────────

def test_yes_button_template_matches_custom_id():
    btn = PolicyVoteYesButton(42)
    assert PolicyVoteYesButton.__discord_ui_compiled_template__.fullmatch(btn.item.custom_id or "")


def test_no_button_template_matches_custom_id():
    btn = PolicyVoteNoButton(42)
    assert PolicyVoteNoButton.__discord_ui_compiled_template__.fullmatch(btn.item.custom_id or "")


def test_abstain_button_template_matches_custom_id():
    btn = PolicyVoteAbstainButton(42)
    assert PolicyVoteAbstainButton.__discord_ui_compiled_template__.fullmatch(btn.item.custom_id or "")


# ── Jail modal structural constraints ─────────────────────────────────

def test_jail_modal_has_title():
    assert _JailModal.title


def test_jail_modal_has_duration_and_reason_inputs():
    assert hasattr(_JailModal, "duration_input")
    assert hasattr(_JailModal, "reason_input")


def test_jail_modal_input_max_lengths():
    assert _JailModal.duration_input.max_length is None or _JailModal.duration_input.max_length <= 4000
    assert _JailModal.reason_input.max_length is None or _JailModal.reason_input.max_length <= 4000


# ── Community ballot buttons ──────────────────────────────────────────
#
# A ballot runs for days in a public thread, so its buttons must survive every
# restart in between — that is what DynamicItem plus the `add_dynamic_items`
# registration in `JailCog.cog_load` buys. These pin the custom-id shape those
# templates have to keep matching.

_BALLOT_BUTTONS = [
    PolicyBallotYesButton,
    PolicyBallotNoButton,
    PolicyBallotAbstainButton,
    PolicyBallotCloseButton,
]


@pytest.mark.parametrize("cls", _BALLOT_BUTTONS, ids=lambda c: c.__name__)
def test_ballot_button_template_matches_its_own_custom_id(cls):
    btn = cls(42)
    assert cls.__discord_ui_compiled_template__.fullmatch(btn.item.custom_id or "")


@pytest.mark.parametrize("cls", _BALLOT_BUTTONS, ids=lambda c: c.__name__)
def test_ballot_button_label_within_discord_limit(cls):
    assert len(cls(1).item.label or "") <= 80


def test_ballot_custom_ids_carry_the_ballot_id():
    assert PolicyBallotYesButton(10).item.custom_id == "policy_ballot:yes:10"
    assert PolicyBallotNoButton(10).item.custom_id == "policy_ballot:no:10"
    assert PolicyBallotAbstainButton(10).item.custom_id == "policy_ballot:abstain:10"
    assert PolicyBallotCloseButton(10).item.custom_id == "policy_ballot:close:10"


@pytest.mark.parametrize("cls", _BALLOT_BUTTONS, ids=lambda c: c.__name__)
def test_ballot_templates_never_match_a_policy_vote_id(cls):
    """`policy_ballot:` and `policy_vote:` share a prefix up to the underscore.
    A template that matched the other feature's ids would route a mod's vote
    press into the ballot handler (and vice versa) after a restart."""
    for vote_id in (
        "policy_vote:yes:10", "policy_vote:no:10", "policy_vote:abstain:10",
    ):
        assert not cls.__discord_ui_compiled_template__.fullmatch(vote_id)


@pytest.mark.parametrize(
    "cls",
    [PolicyVoteYesButton, PolicyVoteNoButton, PolicyVoteAbstainButton],
    ids=lambda c: c.__name__,
)
def test_policy_vote_templates_never_match_a_ballot_id(cls):
    for ballot_id in (
        "policy_ballot:yes:10", "policy_ballot:no:10", "policy_ballot:abstain:10",
        "policy_ballot:close:10",
    ):
        assert not cls.__discord_ui_compiled_template__.fullmatch(ballot_id)


def test_ballot_view_carries_all_four_buttons_while_open():
    from bot_modules.commands.jail_commands import _ballot_view

    ids = [item.custom_id for item in _ballot_view(7).children]
    assert ids == [
        "policy_ballot:yes:7",
        "policy_ballot:no:7",
        "policy_ballot:abstain:7",
        "policy_ballot:close:7",
    ]


def test_ballot_view_is_empty_once_closed():
    """A closed ballot's card keeps its tally but loses its buttons, so a late
    press cannot land on a frozen result."""
    from bot_modules.commands.jail_commands import _ballot_view

    assert list(_ballot_view(7, closed=True).children) == []
