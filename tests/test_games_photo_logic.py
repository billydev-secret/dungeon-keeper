"""Tests for the Photo Challenge bank-pull helper.

Photo Challenge prompts live in the DB question bank (``games_question_bank``,
``game_type='photo'``) and are curated in the web Games Studio — there is no
static prompt bank. The only standalone pure logic is
``get_photo_prompt`` (bank-only, no AI fallback), exercised here against a
fake db. Questions carry a JSON ``tags`` array; the reserved ``nsfw`` tag is
excluded unless a caller opts in. The card rendering and thread flow reuse the
same helpers covered by the FFA/confessions tests.
"""

from __future__ import annotations

import asyncio
import json

from bot_modules.games.utils.question_source import get_photo_prompt


class _FakeDB:
    """Minimal async db stub matching the fetchall surface used by
    ``_get_bank_question`` (now selects ``question_text, tags``)."""

    def __init__(self, rows: list[tuple[str, list[str], str]]):
        # rows: (game_type, tags_list, question_text)
        self._rows = rows

    async def fetchall(self, sql: str, params: tuple):
        (game_type,) = params
        return [
            (r[2], json.dumps(r[1]))
            for r in self._rows
            if r[0] == game_type
        ]


def _run(coro):
    return asyncio.run(coro)


def test_empty_bank_returns_none():
    db = _FakeDB([])
    assert _run(get_photo_prompt(db)) is None
    assert _run(get_photo_prompt(db, tags=["nsfw"])) is None


def test_excludes_nsfw_unless_opted_in():
    db = _FakeDB([
        ("photo", [], "Show us your desk right now."),
        ("photo", ["nsfw"], "Spicy challenge."),
        ("wyr", [], "Not a photo prompt."),
    ])
    # No tag filter → only the non-nsfw prompt.
    for _ in range(25):
        assert _run(get_photo_prompt(db)) == "Show us your desk right now."
    # Opt into nsfw → only the nsfw-tagged prompt qualifies under ANY-match.
    for _ in range(25):
        assert _run(get_photo_prompt(db, tags=["nsfw"])) == "Spicy challenge."


def test_tag_filter_any_match():
    db = _FakeDB([
        ("photo", ["food"], "Photo of your lunch."),
        ("photo", ["pets"], "Photo of your pet."),
        ("photo", [], "Untagged photo."),
    ])
    # food filter → only the food-tagged prompt.
    seen = {_run(get_photo_prompt(db, tags=["food"])) for _ in range(30)}
    assert seen == {"Photo of your lunch."}
    # food OR pets → both tagged prompts (untagged excluded).
    seen = {_run(get_photo_prompt(db, tags=["food", "pets"])) for _ in range(60)}
    assert seen == {"Photo of your lunch.", "Photo of your pet."}


def test_tag_filter_miss_returns_none():
    db = _FakeDB([("photo", ["food"], "Photo of your lunch.")])
    assert _run(get_photo_prompt(db, tags=["nope"])) is None


def test_ignores_other_game_types():
    db = _FakeDB([("wyr", [], "Not a photo prompt.")])
    assert _run(get_photo_prompt(db)) is None


def test_returns_one_of_several_candidates():
    prompts = {"A photo.", "B photo.", "C photo."}
    db = _FakeDB([("photo", [], p) for p in prompts])
    seen = {_run(get_photo_prompt(db)) for _ in range(50)}
    assert seen <= prompts and seen  # every pick is a real candidate
