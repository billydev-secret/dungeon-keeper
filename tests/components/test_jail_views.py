"""Tier 2 component tests: jail/policy View/Button/Modal structural assertions."""

from __future__ import annotations

import pytest

from commands.jail_commands import (
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
