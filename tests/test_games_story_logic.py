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
    format_skip_notice,
    format_story_opening,
    pick_current_player,
    remove_player,
    resolve_starter,
    should_end_after_skip,
)
from bot_modules.core.branding import SECTION_SPACER


def _unspaced(value: str | None) -> str:
    """A field value without the trailing spacer ``apply_section_spacing`` adds.

    Stacked fields carry ``SECTION_SPACER`` for breathing room
    (docs/embed_style_guide.md § Section spacing). These tests assert content,
    not spacing, so they compare against the value with it removed.
    """
    text = value or ""
    return text[: -len(SECTION_SPACER)] if text.endswith(SECTION_SPACER) else text



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


# ── format_story_opening / format_skip_notice (mention safety) ──────


def test_format_story_opening_neutralizes_role_mention():
    """A host-supplied starter carrying a role mention must not survive as a
    live ping in the announcement line."""
    out = format_story_opening("beware <@&123456789012345678>!")
    assert "<@&123456789012345678>" not in out
    assert "​" in out  # escape_mentions inserted a zero-width break


def test_format_story_opening_neutralizes_everyone():
    out = format_story_opening("hello @everyone")
    assert "@everyone" not in out


def test_format_story_opening_escapes_markdown():
    out = format_story_opening("look *here*")
    assert "\\*here\\*" in out


def test_format_skip_notice_neutralizes_mention():
    out = format_skip_notice("<@&123456789012345678>")
    assert "<@&123456789012345678>" not in out
    assert "was skipped" in out


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
    by_name = {(f.name or ""): _unspaced(f.value) for f in embed.fields}
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
    by_name = {(f.name or ""): _unspaced(f.value) for f in embed.fields}
    assert by_name["Progress"] == "Sentence 3/10"
    assert "Alice" in by_name["Currently Writing"]


def test_build_turn_embed_highlights_active_writer_in_order():
    embed = build_turn_embed(
        sentence_count=0,
        max_sentences=5,
        current_player_id=2,
        turn_order=[1, 2, 3],
        name_resolver={1: "Alice", 2: "Bob", 3: "Carol"}.__getitem__,
    )
    by_name = {(f.name or ""): _unspaced(f.value) for f in embed.fields}
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
    by_name = {(f.name or ""): _unspaced(f.value) for f in embed.fields}
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
    by_name = {(f.name or ""): _unspaced(f.value) for f in embed.fields}
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
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import discord  # noqa: E402

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

def test_lobby_embed_renders_the_start_countdown():
    # start_in advertises a start time as a live Discord relative timestamp;
    # the host still presses the button.
    embed = build_lobby_embed(host_name="Alice", visibility="blind", max_sentences=10, start_at=1_700_000_000)
    field = next(f for f in embed.fields if f.name == "⏰ Starting")
    assert field.value == "<t:1700000000:R>"


def test_lobby_embed_omits_the_countdown_when_none_was_set():
    embed = build_lobby_embed(host_name="Alice", visibility="blind", max_sentences=10)
    assert all(f.name != "⏰ Starting" for f in embed.fields)


# ── the turn panel is deleted, not left behind (ephemeral-UI audit E3) ──

import asyncio  # noqa: E402


class _SpyMessage:
    """A sent message that records whether it was deleted or merely edited."""

    def __init__(self) -> None:
        self.delete = AsyncMock()
        self.edit = AsyncMock()


class _SpyChannel:
    """Hands back a distinct spy per send so the turn panel is identifiable."""

    id = 100
    guild = None

    def __init__(self) -> None:
        self.sent: list[_SpyMessage] = []

    async def send(self, *args, **kwargs) -> _SpyMessage:
        msg = _SpyMessage()
        self.sent.append(msg)
        return msg


class _InstantTurnView:
    """StoryTurnView stand-in whose player has already written their line."""

    def __init__(self, *args, **kwargs) -> None:
        self._submitted_event = asyncio.Event()
        self._submitted_event.set()
        self._submitted_text = "And then the door opened."
        self._skipped = False
        self._left: set[int] = set()


async def test_run_story_deletes_the_spent_turn_panel(monkeypatch, sync_db_path):
    """A resolved turn takes its panel with it.

    Story used to disable the buttons and edit the message, leaving one
    dead "it's your turn!" panel per player per round interleaved with the
    story itself — the single biggest contributor to channel clutter the
    ephemeral-UI audit found.
    """
    monkeypatch.setattr(story_cog, "StoryTurnView", _InstantTurnView)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "story", payload={"players": [1]})
    bot.active_views[gid] = object()
    cog = story_cog.StoryCog(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog, "_reveal_story", AsyncMock())
    channel = _SpyChannel()

    await cog._run_story(
        None, gid,
        {"host_id": 1, "players": [1], "max_sentences": 2},
        channel,
    )

    # 0 = the story opening, 1 = the turn panel, 2 = the written sentence.
    turn_panel = channel.sent[1]
    assert turn_panel.delete.await_count == 1
    assert turn_panel.edit.await_count == 0


# ── pacing: 120 s turns, writer skip, drops, leave, host kept (anon-tail-73/77/78) ──

from bot_modules.games_story.logic import (  # noqa: E402
    DEFAULT_TURN_SECONDS,
    MAX_CONSECUTIVE_MISSES,
    WRITER_SKIP_AFTER_SECONDS,
    format_drop_notice,
    format_leave_notice,
    note_turn_outcome,
    roster_for_payout,
    rotation_after_turn,
    should_drop_writer,
    writer_may_skip,
    writer_skip_unlock_at,
)


def test_default_turn_is_two_minutes():
    # Was 300 s: one AFK writer cost two five-minute holes in a fifteen-minute game.
    assert DEFAULT_TURN_SECONDS == 120
    assert story_cog._TURN_TIMEOUT == DEFAULT_TURN_SECONDS
    assert 0 < WRITER_SKIP_AFTER_SECONDS < DEFAULT_TURN_SECONDS


@pytest.mark.parametrize(
    ("order", "index", "remove", "expected"),
    [
        pytest.param([1, 2, 3], 0, (), ([1, 2, 3], 1), id="plain-advance"),
        pytest.param([1, 2, 3], 2, (), ([1, 2, 3], 0), id="wraps-at-the-end"),
        pytest.param([1, 2, 3], 1, {2}, ([1, 3], 1), id="current-writer-dropped-next-keeps-place"),
        pytest.param([1, 2, 3], 0, {2}, ([1, 3], 1), id="next-writer-dropped-skips-to-the-one-after"),
        pytest.param([1, 2, 3], 2, {1}, ([2, 3], 0), id="wrap-onto-a-dropped-writer"),
        pytest.param([1, 2], 1, {1, 2}, ([], 0), id="everyone-gone"),
        pytest.param([], 0, (), ([], 0), id="empty"),
    ],
)
def test_rotation_after_turn(order, index, remove, expected):
    assert rotation_after_turn(order, index, remove) == expected


def test_rotation_after_turn_does_not_mutate_the_order():
    order = [1, 2, 3]
    rotation_after_turn(order, 0, {2})
    assert order == [1, 2, 3]


def test_two_consecutive_misses_drop_a_writer_and_a_sentence_resets():
    misses: dict[int, int] = {}
    assert note_turn_outcome(misses, 7, missed=True) == 1
    assert not should_drop_writer(misses, 7)
    assert note_turn_outcome(misses, 7, missed=False) == 0
    assert note_turn_outcome(misses, 7, missed=True) == 1
    assert note_turn_outcome(misses, 7, missed=True) == MAX_CONSECUTIVE_MISSES
    assert should_drop_writer(misses, 7)
    assert not should_drop_writer(misses, 8)  # never seen: never dropped


@pytest.mark.parametrize(
    ("presser", "elapsed", "expected"),
    [
        pytest.param(2, WRITER_SKIP_AFTER_SECONDS, True, id="writer-after-the-window"),
        pytest.param(2, WRITER_SKIP_AFTER_SECONDS - 1, False, id="writer-too-early"),
        pytest.param(99, WRITER_SKIP_AFTER_SECONDS + 100, False, id="a-non-writer-never"),
    ],
)
def test_writer_may_skip(presser, elapsed, expected):
    assert writer_may_skip(presser, turn_order=[1, 2, 3], opened_at=1000.0, now=1000.0 + elapsed) is expected


def test_writer_skip_unlock_at_is_the_window_after_open():
    assert writer_skip_unlock_at(1000.0) == 1000 + WRITER_SKIP_AFTER_SECONDS


@pytest.mark.parametrize(
    ("players", "sentences", "left", "expected"),
    [
        pytest.param([1, 2, 3], [], set(), [1, 2, 3], id="nobody-left"),
        pytest.param([1, 2, 3], [], {2}, [1, 3], id="left-without-writing-is-unpaid"),
        pytest.param([1, 2, 3], [{"author_id": 2, "text": "x"}], {2}, [1, 2, 3], id="left-after-writing-still-paid"),
    ],
)
def test_roster_for_payout(players, sentences, left, expected):
    assert roster_for_payout(players, sentences, left) == expected


def test_drop_and_leave_notices_neutralize_mentions():
    assert "@\u200beveryone" in format_drop_notice("@everyone")
    assert "missed 2 turns" in format_drop_notice("Bob")
    assert "@\u200bhere" in format_leave_notice("@here")


async def test_sentence_modal_times_out_with_the_turn_and_hands_the_view_its_sentence():
    """A dismissed modal used to leave the button callback parked on
    modal.wait() forever (anon-tail-78). The modal now carries the turn's
    timeout and on_submit sets the view's event directly."""
    bot = _SpyBot(":memory:")
    view = story_cog.StoryTurnView("g", 1, 2, "", bot.games_db, bot, turn_order=[1, 2])
    modal = story_cog.StorySentenceModal("g", 2, "prev", turn_view=view)
    assert modal.timeout == story_cog._TURN_TIMEOUT

    modal.sentence._value = "And then it rained."  # what Discord fills in on submit
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2, display_name="W"), channel=None,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await modal.on_submit(interaction)  # type: ignore[arg-type]

    assert view._submitted_event.is_set()
    assert view._submitted_text == "And then it rained."
    assert view.is_finished()


def _press(user_id: int, *, admin: bool = False):
    perms = SimpleNamespace(administrator=admin, manage_guild=admin)
    if admin:
        # is_host_or_mod only honours perms on a real Member inside a guild.
        user = MagicMock(spec=discord.Member)
        user.id = user_id
        user.display_name = f"U{user_id}"
        user.guild_permissions = perms
        guild = SimpleNamespace(get_member=lambda uid: None)
    else:
        user = SimpleNamespace(id=user_id, display_name=f"U{user_id}", guild_permissions=perms)
        guild = None
    return SimpleNamespace(
        user=user, guild=guild, channel=None, channel_id=None,
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(id=1, edit=AsyncMock(), embeds=[]),
    )


async def test_any_writer_can_skip_once_the_window_has_passed_and_not_before():
    bot = _SpyBot(":memory:")
    view = story_cog.StoryTurnView("g", 1, 2, "", bot.games_db, bot, turn_order=[1, 2, 3])

    early = _press(3)
    await view.skip.callback(early)  # type: ignore[arg-type]
    assert early.response.send_message.await_args.args[0].startswith("❌")
    assert not view._skipped

    view.opened_at -= WRITER_SKIP_AFTER_SECONDS + 1
    late = _press(3)
    await view.skip.callback(late)  # type: ignore[arg-type]
    assert view._skipped and view._submitted_event.is_set()

    stranger = _press(42)
    other = story_cog.StoryTurnView("g", 1, 2, "", bot.games_db, bot, turn_order=[1, 2, 3])
    other.opened_at -= WRITER_SKIP_AFTER_SECONDS + 1
    await other.skip.callback(stranger)  # type: ignore[arg-type]
    assert stranger.response.send_message.await_args.args[0].startswith("❌")
    assert not other._skipped


async def test_leave_marks_the_writer_and_skips_when_it_is_their_turn():
    bot = _SpyBot(":memory:")
    view = story_cog.StoryTurnView("g", 1, 2, "", bot.games_db, bot, turn_order=[1, 2, 3])

    bystander = _press(3)
    await view.leave.callback(bystander)  # type: ignore[arg-type]
    assert view._left == {3} and not view._skipped

    current = _press(2)
    await view.leave.callback(current)  # type: ignore[arg-type]
    assert view._left == {2, 3} and view._skipped and view._submitted_event.is_set()

    outsider = _press(99)
    await view.leave.callback(outsider)  # type: ignore[arg-type]
    assert outsider.response.send_message.await_args.args[0] == "You're not in this story's rotation."


async def test_start_by_a_mod_keeps_the_lobby_host(monkeypatch, sync_db_path):
    """anon-tail-77: whoever pressed Start became the host for Skip purposes,
    so a mod starting on the host's behalf took the host's Skip away."""
    bot = _SpyBot(sync_db_path)
    cog = story_cog.StoryCog(bot)  # type: ignore[arg-type]
    run = AsyncMock()
    monkeypatch.setattr(cog, "_run_story", run)
    gid = await create_game(bot.games_db, 100, 1, "story", state="joining", payload={"players": [1, 2]})
    view = story_cog.StoryJoinView(gid, 1, bot.games_db, bot, cog)

    await view.start_story.callback(_press(99, admin=True))  # type: ignore[arg-type]

    assert run.await_count == 1
    assert run.await_args.kwargs["host_id"] == 1
    assert "host_id" not in run.await_args.args[2]


class _RecordingChannel:
    id = 100
    guild = None

    def __init__(self) -> None:
        self.sent: list[tuple[tuple, dict]] = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())

    @property
    def texts(self) -> list[str]:
        return [str(a[0]) if a else str(k.get("content", "")) for a, k in self.sent]


def _scripted_turn_view(*, misses: set[int], leaves: set[int] = frozenset()):
    """A StoryTurnView stand-in: writers in ``misses`` never write, writers in
    ``leaves`` press Leave without writing, everyone else writes at once."""

    class _View:
        def __init__(self, game_id, host_id, current_player_id, context_text, db, bot, turn_order=None):
            self._submitted_event = asyncio.Event()
            self._submitted_event.set()
            self._left: set[int] = set()
            self.turn_order = list(turn_order or [])
            if current_player_id in leaves:
                self._left.add(current_player_id)
                self._skipped, self._submitted_text = True, None
            elif current_player_id in misses:
                self._skipped, self._submitted_text = True, None
            else:
                self._skipped, self._submitted_text = False, f"line by {current_player_id}."

    return _View


async def test_a_writer_who_misses_two_turns_is_dropped_and_the_story_goes_on(monkeypatch, sync_db_path):
    monkeypatch.setattr(story_cog, "StoryTurnView", _scripted_turn_view(misses={2}))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "story", payload={"players": [1, 2]})
    bot.active_views[gid] = object()
    cog = story_cog.StoryCog(bot)  # type: ignore[arg-type]
    reveal = AsyncMock()
    monkeypatch.setattr(cog, "_reveal_story", reveal)
    channel = _RecordingChannel()

    await cog._run_story(None, gid, {"players": [1, 2], "max_sentences": 6}, channel, host_id=1)

    assert any("missed 2 turns" in t for t in channel.texts)
    assert not any("All writers were skipped" in t for t in channel.texts)
    assert reveal.await_args is not None
    sentences = reveal.await_args.args[2]
    assert len(sentences) == 6  # the starter plus five written lines
    assert {s["author_id"] for s in sentences[1:]} == {1}
    # Dropped for missing turns is not leaving: the writer is still on the roster.
    assert reveal.await_args.args[3] == [1, 2]


async def test_a_writer_who_leaves_without_writing_is_dropped_and_unpaid(monkeypatch, sync_db_path):
    monkeypatch.setattr(story_cog, "StoryTurnView", _scripted_turn_view(misses=set(), leaves={2}))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "story", payload={"players": [1, 2]})
    bot.active_views[gid] = object()
    cog = story_cog.StoryCog(bot)  # type: ignore[arg-type]
    reveal = AsyncMock()
    monkeypatch.setattr(cog, "_reveal_story", reveal)
    channel = _RecordingChannel()

    await cog._run_story(None, gid, {"players": [1, 2], "max_sentences": 4}, channel, host_id=1)

    assert any("left the story" in t for t in channel.texts)
    assert reveal.await_args is not None
    assert reveal.await_args.args[3] == [1]
    assert {s["author_id"] for s in reveal.await_args.args[2][1:]} == {1}


async def test_an_all_miss_lap_still_ends_the_story(monkeypatch, sync_db_path):
    monkeypatch.setattr(story_cog, "StoryTurnView", _scripted_turn_view(misses={1, 2}))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "story", payload={"players": [1, 2]})
    bot.active_views[gid] = object()
    cog = story_cog.StoryCog(bot)  # type: ignore[arg-type]
    reveal = AsyncMock()
    monkeypatch.setattr(cog, "_reveal_story", reveal)
    channel = _RecordingChannel()

    await cog._run_story(None, gid, {"players": [1, 2], "max_sentences": 10}, channel, host_id=1)

    assert any("All writers were skipped" in t for t in channel.texts)
    assert reveal.await_count == 1
