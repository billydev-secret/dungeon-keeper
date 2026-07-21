"""Tests for the extracted Risky Rolls pure-logic modules.

Covers:

- ``services/risky_roll/logic.py`` — serialization helpers, RNG-driven
  tie rolloff loop, the cog-extracted helpers (``normalize_auto_close``,
  ``collect_channel_state_ids``), and the pure prompt-state builders
  that views.py used to own.
- ``services/risky_roll/models.py`` — dataclass methods, the
  ``RiskyRollState.resolve()`` round closer (all six ``RoundResult``
  branches), and the two derived ``PendingQuestionState`` accessors.
- ``services/risky_roll/formatters.py`` — content + embed builders.
  The two async Discord helpers (``get_text_channel``,
  ``post_rolloff_embed``) are out of scope: they're network calls
  with no pure logic worth mocking.
- ``services/risky_roll/state.py`` — module-level globals, lock
  cache identity, default constants.
- ``services/risky_roll/store.py`` — sqlite round-trip integration
  tests against the ``sync_db_path`` fixture.

Mirrors the pressure_cooker / starboard / games_traditional pattern:
extracted pieces are proven to work without spinning up Discord.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot_modules.services.risky_roll import state
from bot_modules.services import embeds as embed_colors
from bot_modules.services.risky_roll import formatters
from bot_modules.services.risky_roll.formatters import (
    build_embed,
    build_how_to_play_content,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_question_reply_content,
    build_rolloff_embed,
    format_lowest_rolloff_note,
    format_user_mentions,
    resolve_embed_accent,
)
from bot_modules.services.risky_roll.logic import (
    build_main_prompt_state,
    build_one_rule_prompt_state,
    collect_channel_state_ids,
    deserialize_user_ids,
    normalize_auto_close_options,
    run_tie_rolloff,
    serialize_user_ids,
)
from bot_modules.services.risky_roll.models import (
    PendingQuestionState,
    PostedQuestionState,
    PromptKind,
    RiskyRollState,
    RoundResult,
)
from bot_modules.services.risky_roll.store import (
    MAX_GAMES_PER_CHANNEL,
    StateStore,
)

import discord


def _embed_field_map(embed: discord.Embed) -> dict[str, str]:
    """Flatten an embed's fields into ``{name: value}``, skipping any with
    missing names. Pure convenience for assertions — avoids repeating the
    ``f.name or ''`` dance and silences ``str | None`` typing noise."""
    return {f.name: f.value or "" for f in embed.fields if f.name}


# ── logic.serialize_user_ids / deserialize_user_ids ──────────────────


def test_serialize_user_ids_returns_none_for_empty_set():
    assert serialize_user_ids(set()) is None


def test_serialize_user_ids_emits_sorted_comma_join():
    assert serialize_user_ids({3, 1, 2}) == "1,2,3"


def test_deserialize_user_ids_round_trips_a_serialized_value():
    raw = serialize_user_ids({10, 20, 30})
    assert deserialize_user_ids(raw) == {10, 20, 30}


@pytest.mark.parametrize("empty", [None, ""])
def test_deserialize_user_ids_returns_empty_set_for_empty_input(empty):
    assert deserialize_user_ids(empty) == set()


def test_deserialize_user_ids_skips_blank_parts():
    """Defensive: a stray double comma must not raise ValueError."""
    assert deserialize_user_ids("1,,2") == {1, 2}


# ── logic.run_tie_rolloff ────────────────────────────────────────────


def test_run_tie_rolloff_resolves_immediately_when_no_tie(monkeypatch):
    """A single-round rolloff returns after one iteration."""
    rolls = iter([7, 3])
    monkeypatch.setattr(
        "bot_modules.services.risky_roll.logic.random.randint",
        lambda *_a, **_kw: next(rolls),
    )
    winner, rounds = run_tie_rolloff([1, 2])
    assert winner == 1  # higher roll wins by default
    assert len(rounds) == 1
    assert rounds[0] == {1: 7, 2: 3}


def test_run_tie_rolloff_pick_lowest_inverts_the_target(monkeypatch):
    rolls = iter([50, 5])
    monkeypatch.setattr(
        "bot_modules.services.risky_roll.logic.random.randint",
        lambda *_a, **_kw: next(rolls),
    )
    winner, rounds = run_tie_rolloff([1, 2], pick_lowest=True)
    assert winner == 2  # lowest roll "wins" the lowest tiebreak
    assert rounds[0] == {1: 50, 2: 5}


def test_run_tie_rolloff_loops_until_a_unique_winner_emerges(monkeypatch):
    """First round ties, second round breaks it. Both rounds returned."""
    # Round 1: both roll 50 (tie). Round 2: ids 1=9, 2=4. Highest=1.
    rolls = iter([50, 50, 9, 4])
    monkeypatch.setattr(
        "bot_modules.services.risky_roll.logic.random.randint",
        lambda *_a, **_kw: next(rolls),
    )
    winner, rounds = run_tie_rolloff([1, 2])
    assert winner == 1
    assert len(rounds) == 2


def test_run_tie_rolloff_dedupes_contender_input(monkeypatch):
    """Duplicate user IDs in the input are collapsed before rolling."""
    rolls = iter([10, 5])
    monkeypatch.setattr(
        "bot_modules.services.risky_roll.logic.random.randint",
        lambda *_a, **_kw: next(rolls),
    )
    winner, rounds = run_tie_rolloff([1, 1, 2])
    assert winner == 1
    assert set(rounds[0].keys()) == {1, 2}


# ── logic.normalize_auto_close_options ───────────────────────────────


@pytest.mark.parametrize(
    "players,minutes,expected_players,expected_minutes",
    [
        (25, 120, 25, 120),    # in-range values pass through
        (None, None, None, None),
        (0, 0, None, None),
        (1, 1, None, 1),       # 1 player auto-close is meaningless → None
        (2, 1, 2, 1),          # 2 is the minimum players value
        (50, -5, 50, None),    # negative minutes → None
    ],
)
def test_normalize_auto_close_options(
    players, minutes, expected_players, expected_minutes
):
    assert normalize_auto_close_options(players, minutes) == (
        expected_players, expected_minutes,
    )


# ── logic.collect_channel_state_ids ──────────────────────────────────


def _round(game_id: str, channel_id: int) -> RiskyRollState:
    return RiskyRollState(
        channel_id=channel_id, guild_id=1, opener_id=1, game_id=game_id,
    )


def _pending(game_id: str, channel_id: int) -> PendingQuestionState:
    return PendingQuestionState(
        channel_id=channel_id, guild_id=1, winner_id=1,
        participant_user_ids={2}, game_id=game_id,
    )


def _posted(message_id: int, channel_id: int) -> PostedQuestionState:
    return PostedQuestionState(
        message_id=message_id, channel_id=channel_id, guild_id=1,
        asker_id=1, allowed_replier_ids={2}, question_text="?",
    )


def test_collect_channel_state_ids_partitions_by_channel():
    active = {
        "a": _round("a", channel_id=100),
        "b": _round("b", channel_id=200),  # different channel
        "c": _round("c", channel_id=100),
    }
    pending = {
        "p1": _pending("p1", channel_id=100),
        "p2": _pending("p2", channel_id=999),
    }
    posted = {
        50: _posted(50, channel_id=100),
        51: _posted(51, channel_id=200),
    }
    games, questions, messages = collect_channel_state_ids(
        active, pending, posted, channel_id=100,
    )
    assert set(games) == {"a", "c"}
    assert questions == ["p1"]
    assert messages == [50]


def test_collect_channel_state_ids_returns_empty_lists_for_unknown_channel():
    games, questions, messages = collect_channel_state_ids({}, {}, {}, channel_id=42)
    assert (games, questions, messages) == ([], [], [])


# ── logic.build_main_prompt_state ────────────────────────────────────


def _resolved_state(
    *,
    highest: int | None = 10,
    lowest: int | None = 20,
    second_lowest: int | None = None,
    rolls: dict[int, int] | None = None,
    lowest_tie: list[int] | None = None,
) -> RiskyRollState:
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = rolls or {10: 90, 20: 5}
    s.highest_user = highest
    s.lowest_user = lowest
    s.second_lowest_user = second_lowest
    s.lowest_tie_user_ids = set(lowest_tie or [])
    return s


def test_build_main_prompt_state_returns_none_without_highest():
    s = _resolved_state(highest=None)
    assert build_main_prompt_state("g", s, RoundResult.OK) is None


def test_build_main_prompt_state_returns_none_when_ok_but_no_lowest():
    """OK with no lowest should never happen but defensively returns None."""
    s = _resolved_state(lowest=None)
    assert build_main_prompt_state("g", s, RoundResult.OK) is None


def test_build_main_prompt_state_room_prompt_on_sixtynine():
    s = _resolved_state(rolls={10: 69, 20: 5, 30: 7})
    prompt = build_main_prompt_state("g", s, RoundResult.SIXTYNINE)
    assert prompt is not None
    assert prompt.prompt_kind == PromptKind.ROOM
    # Room prompt targets every roller (whole room asks)
    assert prompt.participant_user_ids == {10, 20, 30}
    assert prompt.winner_id == 10
    assert prompt.game_id == "g"


def test_build_main_prompt_state_room_prompt_on_sixtynine_tie():
    """SIXTYNINE_TIE goes down the same room-prompt branch."""
    s = _resolved_state(rolls={10: 69, 20: 69, 30: 5})
    prompt = build_main_prompt_state("g", s, RoundResult.SIXTYNINE_TIE)
    assert prompt is not None
    assert prompt.prompt_kind == PromptKind.ROOM


def test_build_main_prompt_state_direct_prompt_targets_lowest_only():
    s = _resolved_state(highest=10, lowest=20)
    prompt = build_main_prompt_state("g", s, RoundResult.OK)
    assert prompt is not None
    assert prompt.prompt_kind == PromptKind.DIRECT
    assert prompt.participant_user_ids == {20}


def test_build_main_prompt_state_direct_prompt_adds_second_lowest_on_100_rule():
    s = _resolved_state(highest=10, lowest=20, second_lowest=30)
    prompt = build_main_prompt_state("g", s, RoundResult.OK)
    assert prompt is not None
    assert prompt.participant_user_ids == {20, 30}


def test_build_main_prompt_state_carries_lowest_tie_user_ids():
    s = _resolved_state(highest=10, lowest=20, lowest_tie=[20, 21, 22])
    prompt = build_main_prompt_state("g", s, RoundResult.OK)
    assert prompt is not None
    assert prompt.lowest_tie_user_ids == {20, 21, 22}


# ── logic.build_one_rule_prompt_state ────────────────────────────────


def test_build_one_rule_prompt_state_returns_none_when_lowest_did_not_roll_1():
    s = _resolved_state(highest=10, lowest=20, rolls={10: 99, 20: 5})
    assert build_one_rule_prompt_state("g", s) is None


def test_build_one_rule_prompt_state_returns_none_without_winner():
    s = _resolved_state(highest=None, lowest=20, rolls={20: 1})
    assert build_one_rule_prompt_state("g", s) is None


def test_build_one_rule_prompt_state_fires_when_lowest_is_a_1():
    s = _resolved_state(highest=10, lowest=20, rolls={10: 99, 20: 1})
    s.second_highest_user = 30
    prompt = build_one_rule_prompt_state("g", s)
    assert prompt is not None
    assert prompt.prompt_kind == PromptKind.TWO_QUESTIONERS
    assert prompt.game_id == "g:1"  # suffixed so it doesn't collide
    assert prompt.winner_id == 10
    assert prompt.extra_questioner_id == 30
    assert prompt.participant_user_ids == {20}


# ── models.RiskyRollState.add_roll / can_roll ────────────────────────


def test_add_roll_stores_the_value():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.add_roll(42, 77)
    assert s.rolls == {42: 77}


def test_can_roll_blocks_repeat_in_open_round():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.add_roll(42, 50)
    assert s.can_roll(42) is False
    assert s.can_roll(43) is True


def test_can_roll_restricts_to_reroll_set_when_rerolling():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.reroll_user_ids = {1, 2}
    assert s.can_roll(1) is True   # in the reroll set
    assert s.can_roll(3) is False  # not in the reroll set


def test_add_roll_clears_reroll_set_when_all_rerollers_submit():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.reroll_user_ids = {1, 2}
    s.add_roll(1, 5)
    assert s.reroll_user_ids == {1, 2}  # still waiting for 2
    s.add_roll(2, 7)
    assert s.reroll_user_ids == set()  # all submitted → cleared


# ── models.RiskyRollState.prepare_reroll ─────────────────────────────


def test_prepare_reroll_clears_state_for_listed_users_only():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 80, 2: 80, 3: 5}
    s.highest_user = 1
    s.lowest_user = 3
    s.second_lowest_user = 3
    s.second_highest_user = 2
    s.lowest_tie_user_ids = {3}
    s.prepare_reroll([1, 2])
    assert s.reroll_user_ids == {1, 2}
    assert 1 not in s.rolls  # rerollers had rolls removed
    assert 2 not in s.rolls
    assert s.rolls[3] == 5   # non-rerollers untouched
    assert s.highest_user is None
    assert s.lowest_user is None
    assert s.second_lowest_user is None
    assert s.second_highest_user is None
    assert s.lowest_tie_user_ids == set()


# ── models.RiskyRollState.reroll_mentions ────────────────────────────


def test_reroll_mentions_returns_sorted_mention_string():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.reroll_user_ids = {3, 1, 2}
    assert s.reroll_mentions() == "<@1>, <@2>, <@3>"


def test_pending_reroll_mentions_skips_already_rolled():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.reroll_user_ids = {1, 2, 3}
    s.rolls = {2: 50}
    pending = s.pending_reroll_mentions()
    assert "<@1>" in pending and "<@3>" in pending
    assert "<@2>" not in pending


# ── models.RiskyRollState.resolve — six branches ────────────────────


def test_resolve_returns_not_enough_with_one_roll():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 50}
    assert s.resolve().result_type == RoundResult.NOT_ENOUGH


def test_resolve_returns_not_enough_with_zero_rolls():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    assert s.resolve().result_type == RoundResult.NOT_ENOUGH


def test_resolve_waiting_for_rerolls_when_reroll_set_incomplete():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 50, 2: 60}
    s.reroll_user_ids = {1, 3}  # 3 has not rolled yet
    assert s.resolve().result_type == RoundResult.WAITING_FOR_REROLLS


def test_resolve_ok_result_picks_unique_high_and_low():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 90, 2: 50, 3: 5}
    result = s.resolve()
    assert result.result_type == RoundResult.OK
    assert s.highest_user == 1
    assert s.lowest_user == 3
    assert s.is_open is False


def test_resolve_sixtynine_branch():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 69, 2: 50}
    result = s.resolve()
    assert result.result_type == RoundResult.SIXTYNINE
    assert s.highest_user == 1
    assert s.lowest_user is None  # no loser when 69 rolled
    assert s.is_open is False


def test_resolve_sixtynine_tie_runs_rolloff(monkeypatch):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 69, 2: 69, 3: 5}

    monkeypatch.setattr(
        "bot_modules.services.risky_roll.models.run_tie_rolloff",
        lambda ids, **_kw: (max(ids), [{i: 50 for i in ids}]),
    )
    result = s.resolve()
    assert result.result_type == RoundResult.SIXTYNINE_TIE
    assert set(result.rolloff_user_ids) == {1, 2}
    assert s.highest_user == 2  # max of [1, 2] per the stub
    assert s.lowest_user is None
    assert s.is_open is False


def test_resolve_tie_for_highest_runs_rolloff(monkeypatch):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 90, 2: 90, 3: 5}

    # Stub the rolloff: first call (highest tie) → user 1 wins.
    # Second call (lowest tie among remaining) — there's only user 3 so
    # no rolloff happens.
    calls = []

    def fake(ids, pick_lowest=False):
        calls.append((tuple(ids), pick_lowest))
        return (min(ids), [{i: 10 for i in ids}])

    monkeypatch.setattr(
        "bot_modules.services.risky_roll.models.run_tie_rolloff", fake,
    )
    result = s.resolve()
    assert result.result_type == RoundResult.TIE
    assert set(result.rolloff_user_ids) == {1, 2}
    assert s.highest_user == 1
    assert s.lowest_user == 3
    assert s.is_open is False
    assert len(calls) == 1  # only the highest rolloff was needed


def test_resolve_tie_for_lowest_records_tie_users(monkeypatch):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    # 2 and 3 tied for lowest at value 5.
    s.rolls = {1: 90, 2: 5, 3: 5}

    def fake(ids, pick_lowest=False):
        return (max(ids), [{i: 1 for i in ids}])  # pick highest id

    monkeypatch.setattr(
        "bot_modules.services.risky_roll.models.run_tie_rolloff", fake,
    )
    result = s.resolve()
    assert result.result_type == RoundResult.OK
    assert s.highest_user == 1
    assert s.lowest_user == 3  # per the stub
    assert s.lowest_tie_user_ids == {2, 3}
    assert result.lowest_rolloff_user_ids == [2, 3]


# ── models — special-rule branches (100 / 1) ────────────────────────


def test_resolve_100_rule_finds_second_lowest():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 100, 2: 5, 3: 20}
    s.resolve()
    assert s.highest_user == 1
    assert s.lowest_user == 2
    # The winner rolled 100 → also pick second-lowest
    assert s.second_lowest_user == 3


def test_resolve_1_rule_finds_second_highest():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 99, 2: 1, 3: 50}
    s.resolve()
    assert s.highest_user == 1
    assert s.lowest_user == 2
    # The loser rolled 1 → also pick second-highest
    assert s.second_highest_user == 3


def test_resolve_100_rule_skips_when_only_two_players():
    """No third candidate to pick as second-lowest."""
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 100, 2: 5}
    s.resolve()
    assert s.second_lowest_user is None


# ── models — PendingQuestionState properties ─────────────────────────


def test_pending_questions_remaining_default_one():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
    )
    assert s.questions_remaining == 1


def test_pending_questions_remaining_counts_extra_questioner():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
        extra_questioner_id=11,
    )
    assert s.questions_remaining == 2
    s.questioners_asked = {10}
    assert s.questions_remaining == 1
    s.questioners_asked = {10, 11}
    assert s.questions_remaining == 0


def test_pending_allowed_questioners_returns_winner_only_by_default():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
    )
    assert s.allowed_questioners() == {10}


def test_pending_allowed_questioners_includes_extra_when_set():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
        extra_questioner_id=11,
    )
    assert s.allowed_questioners() == {10, 11}


# ── formatters.format_user_mentions ──────────────────────────────────


def test_format_user_mentions_sorted_space_joined():
    assert format_user_mentions({3, 1, 2}) == "<@1> <@2> <@3>"


def test_format_user_mentions_empty_returns_empty_string():
    assert format_user_mentions(set()) == ""


# ── formatters.format_lowest_rolloff_note ───────────────────────────


def test_format_lowest_rolloff_note_empty_when_no_selected():
    assert format_lowest_rolloff_note({1, 2}, None) == ""


def test_format_lowest_rolloff_note_empty_when_no_tie():
    assert format_lowest_rolloff_note({1}, 1) == ""


def test_format_lowest_rolloff_note_includes_arrow():
    note = format_lowest_rolloff_note({1, 2}, 2)
    assert "<@1>" in note and "<@2>" in note
    assert "→" in note


# ── formatters.build_pending_prompt_content ─────────────────────────


def _pending_room() -> PendingQuestionState:
    return PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20, 30},
        game_id="g", prompt_kind=PromptKind.ROOM,
    )


def test_build_pending_prompt_content_room_kind_mentions_winner_and_thread():
    text = build_pending_prompt_content(_pending_room())
    assert "<@10>" in text
    assert "**69**" in text
    assert "thread" in text.lower()


def test_build_pending_prompt_content_direct_kind_targets_participants():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20},
        game_id="g", prompt_kind=PromptKind.DIRECT,
    )
    text = build_pending_prompt_content(s)
    assert "<@10>" in text
    assert "<@20>" in text
    assert "Ask Question" in text


def test_build_pending_prompt_content_direct_kind_with_100_rule_mentions_targets():
    """When the 100 rule fired the prompt has two targets; the content
    should mention both and use the multi-target phrasing."""
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20, 30},
        game_id="g", prompt_kind=PromptKind.DIRECT,
    )
    text = build_pending_prompt_content(s)
    assert "<@20>" in text and "<@30>" in text
    assert "**100**" in text  # the 100 rule wording fires


def test_build_pending_prompt_content_two_questioners_lists_unasked():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
        extra_questioner_id=11,
        prompt_kind=PromptKind.TWO_QUESTIONERS,
    )
    text = build_pending_prompt_content(s)
    assert "<@10>" in text and "<@11>" in text


def test_build_pending_prompt_content_two_questioners_notes_already_asked():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
        extra_questioner_id=11,
        prompt_kind=PromptKind.TWO_QUESTIONERS,
        questioners_asked={10},
    )
    text = build_pending_prompt_content(s)
    # Only id 11 should be in the "can ask" line; 10 in the "already asked" line
    assert "already asked" in text


# ── formatters.build_pending_question_summary ───────────────────────


def test_build_pending_question_summary_room_kind_uses_winner():
    s = _pending_room()
    summary = build_pending_question_summary(s, "What is up?")
    assert "<@10>" in summary
    assert "What is up?" in summary
    assert "69" in summary


def test_build_pending_question_summary_two_questioners_uses_asker_override():
    s = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="g",
        extra_questioner_id=11,
        prompt_kind=PromptKind.TWO_QUESTIONERS,
    )
    summary = build_pending_question_summary(s, "Q?", asker_id=11)
    assert "<@11>" in summary
    assert "<@20>" in summary


# ── formatters.build_embed ───────────────────────────────────────────


def test_build_embed_open_round_with_no_rolls_says_no_rolls_yet():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    embed = build_embed(s)
    assert embed.title is not None
    assert "Risky Rolls" in embed.title
    by_name = _embed_field_map(embed)
    assert "Rolls (0)" in by_name
    assert by_name["Rolls (0)"] == "No rolls yet."


def test_build_embed_open_round_lists_rolls_sorted_descending():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 30, 2: 80, 3: 50}
    embed = build_embed(s)
    by_name = _embed_field_map(embed)
    val = by_name["Rolls (3)"]
    # Order: 80 → 50 → 30
    assert val.index("**80**") < val.index("**50**") < val.index("**30**")


def test_build_embed_closed_round_with_winner_shows_result_field():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 90, 2: 5}
    s.highest_user = 1
    s.lowest_user = 2
    s.is_open = False
    embed = build_embed(s)
    by_name = _embed_field_map(embed)
    assert "Result" in by_name
    assert "<@1>" in by_name["Result"]
    assert "<@2>" in by_name["Result"]


def test_build_embed_69_winner_no_loser_shows_room_as_answer():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 69, 2: 5}
    s.highest_user = 1
    s.lowest_user = None
    s.is_open = False
    embed = build_embed(s)
    by_name = _embed_field_map(embed)
    assert "the room" in by_name["Result"]


def test_build_embed_100_rule_shows_two_answerers():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 100, 2: 5, 3: 20}
    s.highest_user = 1
    s.lowest_user = 2
    s.second_lowest_user = 3
    s.is_open = False
    embed = build_embed(s)
    by_name = _embed_field_map(embed)
    assert "<@3>" in by_name["Result"]


def test_build_embed_1_rule_shows_two_askers():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 90, 2: 1, 3: 50}
    s.highest_user = 1
    s.lowest_user = 2
    s.second_highest_user = 3
    s.is_open = False
    embed = build_embed(s)
    by_name = _embed_field_map(embed)
    assert "<@3>" in by_name["Result"]


def test_build_embed_reroll_state_shows_pending():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.reroll_user_ids = {1, 2}
    s.rolls = {2: 7}  # only player 2 has rerolled
    embed = build_embed(s)
    by_name = _embed_field_map(embed)
    assert "Reroll" in " ".join(by_name.keys())


def test_build_embed_footer_describes_auto_close():
    s = RiskyRollState(
        channel_id=100, guild_id=1, opener_id=10,
        auto_close_players=25, auto_close_minutes=120,
    )
    embed = build_embed(s)
    assert embed.footer.text is not None
    assert "25" in embed.footer.text
    assert "120" in embed.footer.text


# ── formatters.build_embed color (guild accent + win = green) ────────

_ACCENT = discord.Color(0x8B5CF6)  # a distinctive purple, unlike any old state color


def test_build_embed_winner_no_loser_is_green_regardless_of_accent():
    # A decided winner with no loser marks a WIN → always green, even when an
    # accent is supplied (the accent must not override the win color).
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 69, 2: 5}
    s.highest_user = 1
    s.lowest_user = None
    s.is_open = False
    assert build_embed(s, None, _ACCENT).color == discord.Color(embed_colors.COLOR_GREEN)
    assert build_embed(s).color == discord.Color(embed_colors.COLOR_GREEN)


def test_build_embed_open_round_uses_accent_when_supplied():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    assert build_embed(s, None, _ACCENT).color == _ACCENT


def test_build_embed_open_round_falls_back_to_old_red_without_accent():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    assert build_embed(s).color == discord.Color(0xDC3545)


def test_build_embed_reroll_uses_accent_and_falls_back_to_old_orange():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.reroll_user_ids = {1, 2}
    assert build_embed(s, None, _ACCENT).color == _ACCENT
    assert build_embed(s).color == discord.Color(0xFF9800)


def test_build_embed_round_over_with_loser_uses_accent_and_falls_back_to_greyple():
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 90, 2: 5}
    s.highest_user = 1
    s.lowest_user = 2
    s.is_open = False
    assert build_embed(s, None, _ACCENT).color == _ACCENT
    assert build_embed(s).color == discord.Color(0x546E7A)


# ── formatters.resolve_embed_accent guards ──────────────────────────


async def test_resolve_embed_accent_returns_none_without_guild():
    assert await resolve_embed_accent(None) is None


async def test_resolve_embed_accent_returns_none_without_store(monkeypatch):
    # No store set → no db_path → fall back (None), never touching branding.
    monkeypatch.setattr(state, "store", None, raising=False)
    assert await resolve_embed_accent(object()) is None


async def test_resolve_embed_accent_resolves_via_branding(monkeypatch):
    class _Store:
        db_path = ":memory:"

    async def _fake_resolve(db_path, guild):
        return _ACCENT

    monkeypatch.setattr(state, "store", _Store(), raising=False)
    monkeypatch.setattr(formatters, "resolve_accent_color", _fake_resolve)
    assert await resolve_embed_accent(object()) == _ACCENT


async def test_resolve_embed_accent_swallows_errors(monkeypatch):
    class _Store:
        db_path = ":memory:"

    async def _boom(db_path, guild):
        raise RuntimeError("branding blew up")

    monkeypatch.setattr(state, "store", _Store(), raising=False)
    monkeypatch.setattr(formatters, "resolve_accent_color", _boom)
    # A branding failure must never crash a game — accent falls back to None.
    assert await resolve_embed_accent(object()) is None


# ── formatters.build_embed name resolution ──────────────────────────


@pytest.fixture
def clear_name_cache():
    """The display-name cache is a module global; isolate each test."""
    saved = dict(state.display_names)
    state.display_names.clear()
    yield state.display_names
    state.display_names.clear()
    state.display_names.update(saved)


class _FakeMember:
    def __init__(self, display_name: str):
        self.display_name = display_name


class _FakeGuild:
    def __init__(self, members: dict[int, _FakeMember]):
        self._members = members

    def get_member(self, uid: int):
        return self._members.get(uid)


def test_build_embed_roster_shows_cached_name_as_text(clear_name_cache):
    clear_name_cache[1] = "Billy"
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 51}
    val = _embed_field_map(build_embed(s))["Rolls (1)"]
    assert "Billy" in val
    assert "<@1>" not in val


def test_build_embed_roster_falls_back_to_mention_when_unknown(clear_name_cache):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {999: 51}
    val = _embed_field_map(build_embed(s))["Rolls (1)"]
    # No cache entry and no guild — a raw mention is the last resort.
    assert "<@999>" in val


def test_build_embed_backfills_name_from_guild_and_caches(clear_name_cache):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {7: 51}
    guild = _FakeGuild({7: _FakeMember("Raptor")})
    val = _embed_field_map(build_embed(s, guild))["Rolls (1)"]
    assert "Raptor" in val
    assert clear_name_cache[7] == "Raptor"  # backfilled into the cache


def test_build_embed_escapes_markdown_in_display_name(clear_name_cache):
    clear_name_cache[1] = "**boss**"
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    s.rolls = {1: 51}
    val = _embed_field_map(build_embed(s))["Rolls (1)"]
    assert r"\*\*boss\*\*" in val


# ── formatters.build_question_reply_content ─────────────────────────


def test_build_question_reply_content_threads_question_and_reply():
    posted = PostedQuestionState(
        message_id=42, channel_id=100, guild_id=1, asker_id=10,
        allowed_replier_ids={20}, question_text="Truth?",
    )
    text = build_question_reply_content(posted, replier_id=20, reply_text="Yes.")
    assert "<@10>" in text and "<@20>" in text
    assert "Truth?" in text and "Yes." in text


# ── formatters.build_how_to_play_content ─────────────────────────────


def test_build_how_to_play_content_mentions_each_special_rule():
    text = build_how_to_play_content()
    assert "69" in text and "100" in text and "1" in text
    assert "Roll" in text


# ── formatters.build_rolloff_embed ───────────────────────────────────


def test_build_rolloff_embed_lists_all_rounds_and_winner():
    rounds = [{1: 50, 2: 50}, {1: 80, 2: 5}]
    embed = build_rolloff_embed([1, 2], rounds, winner_id=1)
    by_name = _embed_field_map(embed)
    # Two round fields + winner field
    assert any(name.startswith("Round 1") for name in by_name)
    assert any(name.startswith("Round 2") for name in by_name)
    # Winner field labels as "Rolloff Winner" for highest pick
    assert any("Rolloff Winner" in name for name in by_name)


def test_build_rolloff_embed_uses_lowest_label_when_pick_lowest():
    embed = build_rolloff_embed([1, 2], [{1: 50, 2: 5}], winner_id=2, pick_lowest=True)
    by_name = _embed_field_map(embed)
    assert any("Selected Lowest" in name for name in by_name)


# ── state module-level globals ──────────────────────────────────────


def test_state_locks_return_same_object_for_same_id():
    """Identity contract: callers depend on locking the same id producing
    the same lock object so concurrent acquirers actually serialize."""
    lock_a = state.get_channel_lock(7777)
    lock_b = state.get_channel_lock(7777)
    assert lock_a is lock_b


def test_state_locks_return_different_objects_for_different_ids():
    a = state.get_channel_lock(1111)
    b = state.get_channel_lock(2222)
    assert a is not b


def test_state_message_lock_and_game_lock_are_independent_caches():
    """game_id 100 should not share a lock with channel_id 100."""
    g = state.get_game_lock("100")
    c = state.get_channel_lock(100)
    m = state.get_message_lock(100)
    assert g is not c and c is not m and g is not m


def test_state_default_min_game_seconds_is_30min():
    assert state.DEFAULT_MIN_GAME_SECONDS == 1800


# ── store integration tests (sync_db_path fixture from conftest) ─────


@pytest.fixture
def store(sync_db_path: Path) -> StateStore:
    return StateStore(sync_db_path)


async def test_max_games_per_channel_is_a_module_constant():
    """Defensive: the cog imports this name directly."""
    assert isinstance(MAX_GAMES_PER_CHANNEL, int)
    assert MAX_GAMES_PER_CHANNEL > 0


async def test_store_round_save_load_delete_round_trip(store: StateStore):
    s = RiskyRollState(
        channel_id=100, guild_id=1, opener_id=10,
        message_id=999, auto_close_players=5, auto_close_minutes=10,
    )
    s.rolls = {10: 80, 20: 5}
    s.highest_user = 10
    s.lowest_user = 20
    s.second_lowest_user = 30
    s.reroll_user_ids = {77, 88}

    await store.save_round(s)
    loaded = await store.load_active_rounds()
    assert len(loaded) == 1
    got = loaded[0]
    assert got.game_id == s.game_id
    assert got.channel_id == s.channel_id
    assert got.opener_id == 10
    assert got.message_id == 999
    assert got.auto_close_players == 5
    assert got.auto_close_minutes == 10
    assert got.highest_user == 10
    assert got.lowest_user == 20
    assert got.second_lowest_user == 30
    assert got.reroll_user_ids == {77, 88}
    assert got.rolls == {10: 80, 20: 5}

    await store.delete_round(s.game_id)
    assert await store.load_active_rounds() == []


async def test_store_load_active_rounds_excludes_closed(store: StateStore):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10, is_open=False)
    await store.save_round(s)
    # Closed rounds are not returned by load_active_rounds (WHERE is_open = 1)
    assert await store.load_active_rounds() == []


async def test_store_save_single_roll_persists_independently(store: StateStore):
    s = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    await store.save_round(s)
    await store.save_single_roll(s.game_id, user_id=42, roll=77)
    loaded = await store.load_active_rounds()
    assert loaded[0].rolls == {42: 77}


async def test_store_ping_role_round_trip(store: StateStore):
    assert await store.load_ping_roles() == {}
    await store.set_ping_role(guild_id=1, role_id=12345)
    assert await store.load_ping_roles() == {1: 12345}
    # Updating overwrites
    await store.set_ping_role(guild_id=1, role_id=99999)
    assert await store.load_ping_roles() == {1: 99999}


async def test_store_min_game_time_round_trip(store: StateStore):
    await store.set_min_game_time(guild_id=1, seconds=600)
    assert await store.load_min_game_times() == {1: 600}
    # Setting to None deletes the row
    await store.set_min_game_time(guild_id=1, seconds=None)
    assert await store.load_min_game_times() == {}


async def test_store_max_games_per_channel_round_trip(store: StateStore):
    await store.set_max_games_per_channel(guild_id=1, cap=3)
    assert await store.load_max_games_per_channel() == {1: 3}
    # Setting to None deletes the row
    await store.set_max_games_per_channel(guild_id=1, cap=None)
    assert await store.load_max_games_per_channel() == {}


async def test_store_pending_question_round_trip(store: StateStore):
    pq = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20, 30}, game_id="abc",
        lowest_tie_user_ids={20, 25},
        prompt_kind=PromptKind.DIRECT,
        extra_questioner_id=11,
        questioners_asked={10},
        prompt_message_id=4242,
    )
    await store.save_pending_question(pq)
    loaded = await store.load_pending_questions()
    assert len(loaded) == 1
    got = loaded[0]
    assert got.game_id == "abc"
    assert got.winner_id == 10
    assert got.participant_user_ids == {20, 30}
    assert got.lowest_tie_user_ids == {20, 25}
    # PromptKind is str-Enum — confirm the value survives the sqlite round trip
    assert got.prompt_kind == PromptKind.DIRECT
    assert isinstance(got.prompt_kind, PromptKind)
    assert got.extra_questioner_id == 11
    assert got.questioners_asked == {10}
    assert got.prompt_message_id == 4242

    await store.delete_pending_question("abc")
    assert await store.load_pending_questions() == []


async def test_store_pending_question_round_trip_for_all_prompt_kinds(store: StateStore):
    for kind in PromptKind:
        pq = PendingQuestionState(
            channel_id=100, guild_id=1, winner_id=10,
            participant_user_ids={20}, game_id=f"g-{kind.value}",
            prompt_kind=kind,
        )
        await store.save_pending_question(pq)
    loaded = await store.load_pending_questions()
    kinds = {p.prompt_kind for p in loaded}
    assert kinds == set(PromptKind)


async def test_store_posted_question_round_trip(store: StateStore):
    posted = PostedQuestionState(
        message_id=4242, channel_id=100, guild_id=1, asker_id=10,
        allowed_replier_ids={20, 30}, question_text="Truth?",
        asker_rolled_100=True, target_rolled_1=False,
        created_at=1700000000.0,
    )
    await store.save_posted_question(posted)
    loaded = await store.load_posted_questions()
    assert len(loaded) == 1
    got = loaded[0]
    assert got.message_id == 4242
    assert got.asker_id == 10
    assert got.allowed_replier_ids == {20, 30}
    assert got.question_text == "Truth?"
    assert got.asker_rolled_100 is True
    assert got.target_rolled_1 is False

    await store.delete_posted_question(4242)
    assert await store.load_posted_questions() == []


async def test_store_sweep_old_posted_questions_removes_stale(store: StateStore):
    # An obviously old timestamp (well over 7 days ago).
    old = PostedQuestionState(
        message_id=1, channel_id=100, guild_id=1, asker_id=10,
        allowed_replier_ids={20}, question_text="?",
        created_at=1.0,  # epoch — definitely > 7 days old
    )
    # Fresh (now) — should survive.
    fresh = PostedQuestionState(
        message_id=2, channel_id=100, guild_id=1, asker_id=10,
        allowed_replier_ids={20}, question_text="?",
    )
    await store.save_posted_question(old)
    await store.save_posted_question(fresh)

    swept = await store.sweep_old_posted_questions()
    assert swept == 1
    remaining = await store.load_posted_questions()
    assert [p.message_id for p in remaining] == [2]


async def test_store_delete_guild_data_clears_all_tables(store: StateStore):
    # Populate the guild
    await store.set_ping_role(guild_id=1, role_id=999)
    await store.set_min_game_time(guild_id=1, seconds=60)
    await store.set_max_games_per_channel(guild_id=1, cap=3)
    rnd = RiskyRollState(channel_id=100, guild_id=1, opener_id=10)
    await store.save_round(rnd)
    pq = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=10,
        participant_user_ids={20}, game_id="p",
    )
    await store.save_pending_question(pq)
    posted = PostedQuestionState(
        message_id=42, channel_id=100, guild_id=1, asker_id=10,
        allowed_replier_ids={20}, question_text="?",
    )
    await store.save_posted_question(posted)

    # And a different guild that should NOT be touched
    other = RiskyRollState(channel_id=200, guild_id=2, opener_id=99)
    await store.save_round(other)

    deleted_game_ids = await store.delete_guild_data(guild_id=1)
    assert rnd.game_id in deleted_game_ids
    assert await store.load_ping_roles() == {}
    assert await store.load_min_game_times() == {}
    assert await store.load_max_games_per_channel() == {}
    assert await store.load_pending_questions() == []
    assert await store.load_posted_questions() == []
    # Other guild's data still present
    remaining = await store.load_active_rounds()
    assert {r.guild_id for r in remaining} == {2}
