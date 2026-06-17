"""Tests for the Truth-or-Dare card (FFA) prompt bank.

Covers ``bot_modules/games_ffa/prompts.py`` (prompt bank + picker). The
cog itself reuses the confession bot's anonymous-identity machinery for
replies, which is covered by the confessions tests; FFA's only standalone
pure logic is the prompt picker.
"""

from __future__ import annotations

from bot_modules.games_ffa.prompts import (
    DARE,
    TRUTH,
    TRUTH_NSFW,
    TRUTH_SFW,
    label_for_kind,
    pick_prompt,
)


def test_pick_prompt_truth_returns_truth_label():
    label, text = pick_prompt("truth", nsfw=False)
    assert label == TRUTH
    assert isinstance(text, str) and text


def test_pick_prompt_dare_returns_dare_label():
    label, text = pick_prompt("dare", nsfw=True)
    assert label == DARE
    assert isinstance(text, str) and text


def test_pick_prompt_random_always_returns_a_valid_label():
    for _ in range(50):
        label, text = pick_prompt("random", nsfw=False)
        assert label in (TRUTH, DARE)
        assert text


def test_pick_prompt_sfw_pulls_from_sfw_bank():
    nsfw_only = set(TRUTH_NSFW) - set(TRUTH_SFW)
    for _ in range(50):
        _, text = pick_prompt("truth", nsfw=False)
        assert text not in nsfw_only


def test_pick_prompt_nsfw_pulls_from_nsfw_bank():
    sfw_only = set(TRUTH_SFW) - set(TRUTH_NSFW)
    for _ in range(50):
        _, text = pick_prompt("truth", nsfw=True)
        assert text not in sfw_only


def test_label_for_kind_defaults_to_truth():
    assert label_for_kind("dare") == DARE
    assert label_for_kind("truth") == TRUTH
    assert label_for_kind("random") == TRUTH
