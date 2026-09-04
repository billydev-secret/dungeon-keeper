"""Every question-bank game is bank-only — no AI generation fallback.

Six games (wyr, nhie, mlt, rushmore, price, clapback) used to fall back to an
AI generator when their bank came up empty, driven by prompts edited in the
dashboard's "Prompts & AI" studios. The studios were deleted, and with them
``prompt_config.json`` and the ``get_ai_config``/``_ai_generate`` helpers.

These tests pin the resulting contract: an empty or fully-filtered bank yields
``None``, and the module no longer reaches for an AI client at all. Without
them a re-added fallback would slip in unnoticed, since the *shape* of the
return value (a question or ``None``) is unchanged.

The Discord-facing behaviour on ``None`` lives with each cog: Rushmore says
"No topics in the question bank", Price skips the round.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot_modules.games.utils import question_source
from bot_modules.games.utils.question_source import (
    get_clapback_prompt,
    get_mlt_prompt,
    get_nhie_statement,
    get_price_scenario,
    get_rushmore_topic,
    get_wyr_question,
    normalise_tags,
)


class _FakeDB:
    """Async db stub matching the surface ``_get_bank_question`` uses."""

    def __init__(self, rows: list[tuple[str, list[str], str]]):
        self._rows = rows
        self.served: list[int] = []

    async def fetchall(self, sql: str, params: tuple):
        (game_type,) = params
        return [
            (qid, r[2], json.dumps(r[1]), None)
            for qid, r in enumerate(self._rows)
            if r[0] == game_type
        ]

    async def execute(self, sql: str, params: tuple):
        (qid,) = params
        self.served.append(qid)


def _run(coro):
    return asyncio.run(coro)


# The single-string getters share one contract; wyr returns a tuple and is
# checked separately below.
SINGLE_VALUE_GETTERS = [
    pytest.param(get_nhie_statement, "nhie", id="nhie"),
    pytest.param(get_mlt_prompt, "mlt", id="mlt"),
    pytest.param(get_rushmore_topic, "rushmore", id="rushmore"),
    pytest.param(get_price_scenario, "price", id="price"),
    pytest.param(get_clapback_prompt, "clapback", id="clapback"),
]


@pytest.mark.parametrize("getter,game_type", SINGLE_VALUE_GETTERS)
def test_empty_bank_returns_none(getter, game_type):
    """No bank rows → None. Previously this was the AI fallback's trigger."""
    assert _run(getter(_FakeDB([]))) is None


@pytest.mark.parametrize("getter,game_type", SINGLE_VALUE_GETTERS)
def test_serves_from_bank_when_present(getter, game_type):
    db = _FakeDB([(game_type, [], "a banked question")])
    assert _run(getter(db)) == "a banked question"
    assert db.served == [0]


@pytest.mark.parametrize("getter,game_type", SINGLE_VALUE_GETTERS)
def test_filtered_miss_returns_none(getter, game_type):
    """A tag filter matching nothing is a miss, not an AI-generation trigger."""
    db = _FakeDB([(game_type, ["silly"], "a banked question")])
    assert _run(getter(db, tags=["serious"])) is None


@pytest.mark.parametrize("getter,game_type", SINGLE_VALUE_GETTERS)
@pytest.mark.parametrize(
    "stored_tag",
    [
        pytest.param("nsfw", id="lower"),
        # The 08-28 import wrote "Nsfw"; a case-sensitive gate served every one
        # of those rows in age-unrestricted channels.
        pytest.param("Nsfw", id="title"),
        pytest.param("NSFW", id="upper"),
        pytest.param(" nsfw ", id="padded"),
    ],
)
def test_nsfw_row_excluded_without_channel_opt_in(getter, game_type, stored_tag):
    """NSFW stays gated on the channel flag; an excluded row is still a miss."""
    db = _FakeDB([(game_type, [stored_tag], "spicy")])
    assert _run(getter(db)) is None
    assert _run(getter(db, allow_nsfw=True)) == "spicy"


@pytest.mark.parametrize("getter,game_type", SINGLE_VALUE_GETTERS)
def test_tag_filter_matches_case_insensitively(getter, game_type):
    """A stored "Silly" row is a hit for a requested "silly" (and vice versa)."""
    db = _FakeDB([(game_type, ["Silly"], "a banked question")])
    assert _run(getter(db, tags=["silly"])) == "a banked question"
    db = _FakeDB([(game_type, ["silly"], "a banked question")])
    assert _run(getter(db, tags=["SILLY"])) == "a banked question"


def test_wyr_splits_bank_row_into_two_options():
    db = _FakeDB([("wyr", [], "fight a horse-sized duck|fight duck-sized horses")])
    assert _run(get_wyr_question(db)) == (
        "fight a horse-sized duck",
        "fight duck-sized horses",
    )


def test_wyr_empty_bank_returns_none():
    assert _run(get_wyr_question(_FakeDB([]))) is None


def test_wyr_row_without_separator_is_a_miss():
    """A malformed row can't be split into two options, so it serves nothing."""
    db = _FakeDB([("wyr", [], "no separator here")])
    assert _run(get_wyr_question(db)) is None


def test_module_has_no_ai_generation_surface():
    """The AI helpers and their prompt-config loader are gone for good.

    Asserted by name because a re-added fallback would otherwise be invisible
    to the tests above — they'd still pass if the helper existed but happened
    to return None.
    """
    for gone in ("get_ai_config", "_ai_generate", "_system", "_load_config", "_parse_wyr"):
        assert not hasattr(question_source, gone), f"{gone} should have been removed"
    assert not hasattr(question_source, "generate_text")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, [], id="none-is-empty"),
        pytest.param([], [], id="empty"),
        # The 08-28 import wrote "Nsfw"; the gate matches the literal "nsfw".
        pytest.param(["Nsfw", " Spicy", "NSFW"], ["nsfw", "spicy"], id="case-space-dedupe"),
        pytest.param(["", "  ", "a"], ["a"], id="drops-empties"),
        pytest.param(("Dare", "nsfw", "dare"), ["dare", "nsfw"], id="first-seen-order"),
        pytest.param([1, "1"], ["1"], id="non-strings-coerced"),
    ],
)
def test_normalise_tags_is_the_one_rule(raw, expected):
    """Every tag reader and writer (bank draw, dashboard save/read/filter,
    the prod fix script) shares this rule, so it is pinned once here."""
    assert normalise_tags(raw) == expected
