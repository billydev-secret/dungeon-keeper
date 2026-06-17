"""Tests for the Photo Challenge bank-pull helper.

Photo Challenge prompts live in the DB question bank (``games_question_bank``,
``game_type='photo'``) and are curated in the web Games Studio — there is no
static prompt bank. The only standalone pure logic is
``get_photo_prompt`` (bank-only, no AI fallback), exercised here against a
fake db. The card rendering and thread flow reuse the same helpers covered by
the FFA/confessions tests.
"""

from __future__ import annotations

import asyncio

from bot_modules.games.utils.question_source import get_photo_prompt


class _FakeDB:
    """Minimal async db stub matching the fetchall/fetchone surface used by
    ``_get_bank_question``."""

    def __init__(self, rows: list[tuple[str, str, str]]):
        # rows: (game_type, category, question_text)
        self._rows = rows

    async def fetchall(self, sql: str, params: tuple):
        # Mirror the helper's two query shapes (with / without category).
        if "AND category = ?" in sql:
            game_type, category = params
            return [(r[2],) for r in self._rows if r[0] == game_type and r[1] == category]
        (game_type,) = params
        return [(r[2],) for r in self._rows if r[0] == game_type]


def _run(coro):
    return asyncio.run(coro)


def test_empty_bank_returns_none():
    db = _FakeDB([])
    assert _run(get_photo_prompt(db, "sfw")) is None
    assert _run(get_photo_prompt(db, "nsfw")) is None


def test_pulls_from_matching_category_only():
    db = _FakeDB([
        ("photo", "sfw", "Show us your desk right now."),
        ("photo", "nsfw", "Spicy challenge."),
        ("wyr", "sfw", "Not a photo prompt."),
    ])
    for _ in range(25):
        assert _run(get_photo_prompt(db, "sfw")) == "Show us your desk right now."
        assert _run(get_photo_prompt(db, "nsfw")) == "Spicy challenge."


def test_ignores_other_game_types():
    db = _FakeDB([("wyr", "sfw", "Not a photo prompt.")])
    assert _run(get_photo_prompt(db, "sfw")) is None


def test_returns_one_of_several_candidates():
    prompts = {"A photo.", "B photo.", "C photo."}
    db = _FakeDB([("photo", "sfw", p) for p in prompts])
    seen = {_run(get_photo_prompt(db, "sfw")) for _ in range(50)}
    assert seen <= prompts and seen  # every pick is a real candidate
