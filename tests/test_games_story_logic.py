"""Tests for the extracted Story Builder (Exquisite Corpse) pure-logic modules.

Covers ``bot_modules/games_story/logic.py`` (input clamps, starter
resolution, lobby mutators, turn-order shuffle, current-player lookup,
context-builder for modal prefill, sentence append, skip-end predicate,
story-text assembly + truncation, attribution-line rendering, chunk
splitting) and ``bot_modules/games_story/embeds.py`` (lobby, per-turn,
complete-story, attribution embed builders). Mirrors the
games_ttl / games_traditional pressure-cooker pattern: the cog file
stays thin; this module proves the extracted pieces work without
spinning up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games_story.embeds import (
    build_attribution_embed,
    build_complete_story_embed,
    build_lobby_embed,
    build_turn_embed,
)
from bot_modules.games_story.logic import (
    DEFAULT_STARTER,
    add_player,
    append_sentence,
    assemble_story_text,
    build_attribution_lines,
    build_context,
    build_turn_order,
    chunk_attribution_lines,
    clamp_max_sentences,
    pick_current_player,
    remove_player,
    resolve_starter,
    should_end_after_skip,
)


# ── clamp_max_sentences ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (-100, 2),
        (0, 2),
        (1, 2),
        (2, 2),
        (5, 5),
        (10, 10),
        (30, 30),
        (31, 30),
        (1_000_000, 30),
    ],
)
def test_clamp_max_sentences_bounds(value, expected):
    assert clamp_max_sentences(value) == expected


# ── resolve_starter ─────────────────────────────────────────────────


def test_resolve_starter_uses_default_when_blank():
    assert resolve_starter("") == DEFAULT_STARTER


def test_resolve_starter_uses_default_when_none():
    assert resolve_starter(None) == DEFAULT_STARTER


def test_resolve_starter_passes_through_custom():
    assert resolve_starter("In the year 2099,") == "In the year 2099,"


def test_default_starter_is_nontrivial():
    """Sanity guard: a misconfigured default would silently break the
    opening message in the channel."""
    assert isinstance(DEFAULT_STARTER, str)
    assert len(DEFAULT_STARTER) > 10


# ── add_player / remove_player ──────────────────────────────────────


def test_add_player_creates_players_list():
    payload: dict = {}
    add_player(payload, 42)
    assert payload["players"] == [42]


def test_add_player_is_idempotent():
    payload: dict = {"players": [42]}
    add_player(payload, 42)
    assert payload["players"] == [42]


def test_add_player_appends_to_existing_list():
    payload: dict = {"players": [1, 2]}
    add_player(payload, 3)
    assert payload["players"] == [1, 2, 3]


def test_remove_player_removes_from_list():
    payload: dict = {"players": [1, 2, 3]}
    remove_player(payload, 2)
    assert payload["players"] == [1, 3]


def test_remove_player_silent_when_absent():
    payload: dict = {"players": [1, 2]}
    remove_player(payload, 99)
    assert payload["players"] == [1, 2]


def test_remove_player_handles_missing_key():
    payload: dict = {}
    remove_player(payload, 42)
    assert payload["players"] == []


# ── build_turn_order ────────────────────────────────────────────────


def test_build_turn_order_preserves_all_players():
    rng = random.Random(0)
    order = build_turn_order([1, 2, 3, 4], rng=rng)
    assert sorted(order) == [1, 2, 3, 4]


def test_build_turn_order_does_not_mutate_input():
    players = [1, 2, 3]
    rng = random.Random(0)
    build_turn_order(players, rng=rng)
    assert players == [1, 2, 3]


def test_build_turn_order_uses_module_random_when_rng_omitted():
    order = build_turn_order([1, 2, 3])
    assert sorted(order) == [1, 2, 3]


def test_build_turn_order_with_seeded_rng_is_reproducible():
    a = build_turn_order([1, 2, 3, 4, 5], rng=random.Random(7))
    b = build_turn_order([1, 2, 3, 4, 5], rng=random.Random(7))
    assert a == b


def test_build_turn_order_empty():
    assert build_turn_order([]) == []


# ── pick_current_player ─────────────────────────────────────────────


def test_pick_current_player_zero_index():
    assert pick_current_player([10, 20, 30], 0) == 10


def test_pick_current_player_wraps_modulo():
    order = [10, 20, 30]
    assert pick_current_player(order, 3) == 10
    assert pick_current_player(order, 4) == 20
    # 100 % 3 == 1 -> order[1] == 20
    assert pick_current_player(order, 100) == 20
    assert pick_current_player(order, 100) == order[100 % 3]


# ── build_context ───────────────────────────────────────────────────


def test_build_context_blind_returns_last_only():
    sentences = [
        {"author_id": None, "text": "A"},
        {"author_id": 1, "text": "B"},
        {"author_id": 2, "text": "C"},
    ]
    assert build_context(sentences, "blind") == "C"


def test_build_context_full_joins_all_with_space():
    sentences = [
        {"author_id": None, "text": "A"},
        {"author_id": 1, "text": "B"},
        {"author_id": 2, "text": "C"},
    ]
    assert build_context(sentences, "full") == "A B C"


def test_build_context_empty_returns_empty_string():
    assert build_context([], "blind") == ""
    assert build_context([], "full") == ""


def test_build_context_unknown_visibility_falls_back_to_full():
    """Anything other than 'blind' uses full visibility. This keeps the
    cog's behavior even if a payload was saved with an unexpected value."""
    sentences = [{"author_id": None, "text": "x"}, {"author_id": 1, "text": "y"}]
    assert build_context(sentences, "anything-else") == "x y"


# ── append_sentence ─────────────────────────────────────────────────


def test_append_sentence_creates_list_when_missing():
    payload: dict = {}
    out = append_sentence(payload, 42, "hello")
    assert payload["sentences"] == [{"author_id": 42, "text": "hello"}]
    assert out is payload["sentences"]


def test_append_sentence_appends_to_existing_list():
    payload: dict = {"sentences": [{"author_id": None, "text": "start"}]}
    append_sentence(payload, 7, "next")
    assert payload["sentences"] == [
        {"author_id": None, "text": "start"},
        {"author_id": 7, "text": "next"},
    ]


def test_append_sentence_preserves_none_author_for_narrator():
    payload: dict = {}
    append_sentence(payload, None, "Once upon a time")
    assert payload["sentences"][0]["author_id"] is None


# ── should_end_after_skip ───────────────────────────────────────────


@pytest.mark.parametrize(
    "consecutive,n,expected",
    [
        (0, 3, False),
        (1, 3, False),
        (2, 3, False),
        (3, 3, True),
        (4, 3, True),  # past the bar still ends
        (0, 1, False),
        (1, 1, True),
    ],
)
def test_should_end_after_skip(consecutive, n, expected):
    assert should_end_after_skip(consecutive, n) is expected


# ── assemble_story_text ─────────────────────────────────────────────


def test_assemble_story_text_joins_with_single_space():
    sentences = [
        {"author_id": None, "text": "Once upon a time"},
        {"author_id": 1, "text": "the end."},
    ]
    out = assemble_story_text(sentences)
    assert out == "Once upon a time the end."


def test_assemble_story_text_escapes_markdown():
    """Markdown chars in raw sentences must be backslash-escaped so the
    embed renders the literal text."""
    sentences = [{"author_id": 1, "text": "*bold*"}]
    out = assemble_story_text(sentences)
    assert "\\*" in out


def test_assemble_story_text_truncates_at_default_budget():
    """Description budget is 4090; output is truncated to 4087 chars +
    the single-char ellipsis (mirrors the cog's ``text[:4087] + "…"``
    where the budget is reserved for *3 bytes* of UTF-8 ellipsis)."""
    long = "a" * 10_000
    sentences = [{"author_id": 1, "text": long}]
    out = assemble_story_text(sentences)
    # The ellipsis is a single Python char, so total length is 4088.
    # But the byte-length of the ellipsis is 3 in UTF-8, which is why
    # the cog reserves 3 from the 4090 budget.
    assert len(out) == 4088
    assert out.endswith("…")
    assert out[:4087] == "a" * 4087


def test_assemble_story_text_no_truncation_when_under_budget():
    sentences = [{"author_id": 1, "text": "short"}]
    out = assemble_story_text(sentences)
    assert out == "short"
    assert "…" not in out


def test_assemble_story_text_custom_max_len():
    sentences = [{"author_id": 1, "text": "abcdefghij"}]
    out = assemble_story_text(sentences, max_len=5)
    # 5 - 3 = 2 head chars + single-char ellipsis (3 bytes UTF-8).
    assert out == "ab…"
    assert len(out) == 3


def test_assemble_story_text_empty_returns_empty_string():
    assert assemble_story_text([]) == ""


# ── build_attribution_lines ─────────────────────────────────────────


def test_build_attribution_lines_renders_narrator_for_none_author():
    sentences = [{"author_id": None, "text": "Once upon a time"}]
    lines = build_attribution_lines(sentences, name_resolver=lambda uid: "Should not call")
    assert lines == ["**Narrator:** *Once upon a time*"]


def test_build_attribution_lines_uses_name_resolver():
    sentences = [
        {"author_id": 1, "text": "hi"},
        {"author_id": 2, "text": "yo"},
    ]
    name_map = {1: "Alice", 2: "Bob"}
    lines = build_attribution_lines(sentences, name_resolver=name_map.__getitem__)
    assert lines == ["**Alice:** *hi*", "**Bob:** *yo*"]


def test_build_attribution_lines_escapes_markdown_in_name_and_text():
    """A nickname like '*foo*' or text with backticks must be escaped so
    the rendered embed doesn't accidentally apply markdown to user input."""
    sentences = [{"author_id": 1, "text": "*spicy* `code`"}]
    lines = build_attribution_lines(sentences, name_resolver=lambda uid: "_under_")
    line = lines[0]
    # Name escaped
    assert "\\_under\\_" in line
    # Text escaped
    assert "\\*spicy\\*" in line
    assert "\\`code\\`" in line


def test_build_attribution_lines_empty():
    assert build_attribution_lines([], name_resolver=lambda uid: "x") == []


# ── chunk_attribution_lines ─────────────────────────────────────────


def test_chunk_attribution_lines_single_chunk_under_limit():
    lines = ["a", "b", "c"]
    chunks = chunk_attribution_lines(lines)
    assert chunks == [["a", "b", "c"]]


def test_chunk_attribution_lines_splits_at_field_limit():
    # Each line is 500 chars; 3 lines (~1503 chars w/ newlines) overflow 1024
    line = "x" * 500
    lines = [line, line, line]
    chunks = chunk_attribution_lines(lines)
    # 500 + 1 = 501 (first), 501 + 500 + 1 = 1002 (second fits), 1002+500+1 = 1503 (third overflows)
    # So expect first chunk = 2 lines, second chunk = 1 line.
    assert len(chunks) == 2
    assert len(chunks[0]) == 2
    assert len(chunks[1]) == 1


def test_chunk_attribution_lines_custom_limit():
    lines = ["abc", "def", "ghi"]
    chunks = chunk_attribution_lines(lines, max_field_len=5)
    # Each line "abc" is 3 chars + 1 sep = 4. Adding another (3+1) -> 8 > 5, split.
    assert chunks == [["abc"], ["def"], ["ghi"]]


def test_chunk_attribution_lines_empty_input():
    assert chunk_attribution_lines([]) == []


def test_chunk_attribution_lines_oversized_single_line_kept_intact():
    """A single line longer than the field-limit still gets emitted —
    the cog's accumulator never drops a line, matching prior behavior."""
    big = "x" * 5000
    chunks = chunk_attribution_lines([big])
    # The first iter: current is empty, so the overflow guard's
    # `and current` keeps it; the line goes into the first chunk.
    assert chunks == [[big]]


# ── build_lobby_embed ───────────────────────────────────────────────


def test_build_lobby_embed_has_expected_fields():
    embed = build_lobby_embed(host_name="Alice", visibility="blind", max_sentences=10)
    assert embed.title is not None
    assert "Story Builder" in embed.title
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert "Writers (0)" in by_name
    assert by_name["Writers (0)"] == "—"
    assert by_name["Host"] == "Alice"
    assert "blind" in by_name["Mode"]
    assert "10 sentences" in by_name["Mode"]


def test_build_lobby_embed_has_footer():
    embed = build_lobby_embed(host_name="Alice", visibility="full", max_sentences=5)
    assert embed.footer.text is not None
    assert "Story Builder" in embed.footer.text


# ── build_turn_embed ────────────────────────────────────────────────


def test_build_turn_embed_renders_progress_and_writer():
    embed = build_turn_embed(
        sentence_count=2,
        max_sentences=10,
        current_player_id=1,
        turn_order=[1, 2, 3],
        name_resolver={1: "Alice", 2: "Bob", 3: "Carol"}.__getitem__,
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert by_name["Progress"] == "Sentence 3/10"
    assert "Alice" in by_name["Currently writing"]


def test_build_turn_embed_highlights_active_writer_in_order():
    embed = build_turn_embed(
        sentence_count=0,
        max_sentences=5,
        current_player_id=2,
        turn_order=[1, 2, 3],
        name_resolver={1: "Alice", 2: "Bob", 3: "Carol"}.__getitem__,
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    order_text = by_name["Turn Order"]
    # Bob (active) should be wrapped with ▸ markers and ✍️
    assert "▸ Bob" in order_text
    assert "✍️" in order_text
    # Non-active writers appear plain
    assert "Alice" in order_text
    assert "Carol" in order_text


def test_build_turn_embed_escapes_markdown_in_names():
    embed = build_turn_embed(
        sentence_count=0,
        max_sentences=5,
        current_player_id=1,
        turn_order=[1, 2],
        name_resolver={1: "*tricky*", 2: "_bold_"}.__getitem__,
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    order_text = by_name["Turn Order"]
    assert "\\*tricky\\*" in order_text
    assert "\\_bold\\_" in order_text


# ── build_complete_story_embed ──────────────────────────────────────


def test_build_complete_story_embed_renders_description_and_summary():
    embed = build_complete_story_embed(
        story_text="A short story.",
        player_count=4,
        sentence_count=8,
    )
    assert embed.description == "*A short story.*"
    assert embed.title is not None
    assert "Complete Story" in embed.title
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert "A Community Original" in by_name
    assert "4 writers" in by_name["A Community Original"]
    assert "8 sentences" in by_name["A Community Original"]


# ── build_attribution_embed ─────────────────────────────────────────


def test_build_attribution_embed_single_chunk_unsuffixed():
    embed = build_attribution_embed([["**Alice:** *hi*", "**Bob:** *yo*"]])
    assert embed.title is not None
    assert "Who Wrote What" in embed.title
    names = [f.name for f in embed.fields]
    assert names == ["Sentences"]
    assert embed.fields[0].value is not None
    assert "Alice" in embed.fields[0].value
    assert "Bob" in embed.fields[0].value


def test_build_attribution_embed_multi_chunk_suffixed():
    chunks = [["line1", "line2"], ["line3"], ["line4"]]
    embed = build_attribution_embed(chunks)
    names = [f.name for f in embed.fields]
    assert names == ["Sentences (pt. 1)", "Sentences (pt. 2)", "Sentences (pt. 3)"]


def test_build_attribution_embed_empty_chunks_no_fields():
    embed = build_attribution_embed([])
    assert len(embed.fields) == 0


def test_build_attribution_embed_has_footer():
    embed = build_attribution_embed([["x"]])
    assert embed.footer.text is not None
    assert "Story Builder" in embed.footer.text


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_story_cog as story_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402
from tests.fakes import FakeChannel  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_reveal_story_pays_joined_players(monkeypatch, sync_db_path):
    """The genuine reveal site pays the full joined roster, not just the host."""
    spy = AsyncMock()
    monkeypatch.setattr(story_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "story", payload={"players": [1, 2, 3]})
    cog = story_cog.StoryCog(bot)  # type: ignore[arg-type]
    channel = FakeChannel(id=100)
    sentences = [{"author_id": None, "text": "A"}, {"author_id": 2, "text": "B"}]
    await cog._reveal_story(channel, gid, sentences, [1, 2, 3], None)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3]
    assert call.kwargs["bot"] is bot
