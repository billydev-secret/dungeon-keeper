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


# ── get_ffa_prompt (bank-backed, with code fallback) ──────────────────────────

import asyncio
import json

from bot_modules.games.utils.question_source import get_ffa_prompt


class _FakeDB:
    def __init__(self, rows):
        # rows: (game_type, tags_list, question_text)
        self._rows = rows

    async def fetchall(self, sql, params):
        (game_type,) = params
        return [(r[2], json.dumps(r[1])) for r in self._rows if r[0] == game_type]


def _run(coro):
    return asyncio.run(coro)


def test_get_ffa_prompt_kind_drives_label():
    db = _FakeDB([
        ("ffa", ["truth"], "A truth."),
        ("ffa", ["dare"], "A dare."),
    ])
    assert _run(get_ffa_prompt(db, kind="truth")) == (TRUTH, "A truth.")
    assert _run(get_ffa_prompt(db, kind="dare")) == (DARE, "A dare.")


def test_get_ffa_prompt_excludes_nsfw_unless_allow_nsfw():
    """NSFW is gated on the channel's age-restriction flag (``allow_nsfw``);
    requesting the 'nsfw' tag cannot re-enable it."""
    db = _FakeDB([
        ("ffa", ["truth"], "Tame truth."),
        ("ffa", ["truth", "nsfw"], "Spicy truth."),
    ])
    # Default (no channel opt-in) → nsfw rows are excluded.
    seen = {_run(get_ffa_prompt(db, kind="truth"))[1] for _ in range(40)}
    assert seen == {"Tame truth."}
    # Requesting the 'nsfw' tag without allow_nsfw doesn't re-enable NSFW —
    # only tame content comes back.
    seen = {_run(get_ffa_prompt(db, kind="truth", tags=["nsfw"]))[1] for _ in range(40)}
    assert seen == {"Tame truth."}
    # An nsfw-only pool with the tag requested but no channel opt-in is a
    # filtered miss (no code-bank fallback when a tag filter was supplied).
    nsfw_only = _FakeDB([("ffa", ["truth", "nsfw"], "Spicy truth.")])
    assert _run(get_ffa_prompt(nsfw_only, kind="truth", tags=["nsfw"])) is None
    # Channel opt-in (allow_nsfw=True) → both pools are candidates.
    seen = {_run(get_ffa_prompt(db, kind="truth", allow_nsfw=True))[1] for _ in range(40)}
    assert seen == {"Tame truth.", "Spicy truth."}


def test_get_ffa_prompt_filtered_miss_returns_none():
    db = _FakeDB([("ffa", ["truth"], "Only truth.")])
    assert _run(get_ffa_prompt(db, kind="random", tags=["nope"])) is None


def test_get_ffa_prompt_empty_bank_falls_back_to_code():
    db = _FakeDB([])
    label, text = _run(get_ffa_prompt(db, kind="truth"))
    assert label == TRUTH and isinstance(text, str) and text
