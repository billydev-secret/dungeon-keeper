"""Tests for the extracted Clapback pure-logic modules.

Covers ``bot_modules/games_clapback/logic.py`` (create_matchups,
calculate_matchup_score, find_best_answer_record,
find_closest_matchup_record, sort_scores, shuffled_replay_config,
clamp_config_values) and ``bot_modules/games_clapback/embeds.py``
(lobby/submit/vote/reveal/scoreboard/recap embed builders).

Mirrors the hottakes / ttl extraction pattern: the cog file stays thin;
this module proves the extracted pieces work without spinning up
Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games_clapback.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
    build_scoreboard_embed,
    build_submit_embed,
    build_vote_embed,
)
from bot_modules.games_clapback.logic import (
    AI_SYSTEM_PROMPT,
    AI_USER_PROMPT,
    MAX_PLAYERS,
    MIN_PLAYERS,
    calculate_matchup_score,
    clamp_config_values,
    create_matchups,
    find_best_answer_record,
    find_closest_matchup_record,
    shuffled_replay_config,
    sort_scores,
)


def _name_resolver(uid: int) -> str:
    return f"User{uid}"


# ── constants sanity ─────────────────────────────────────────────────


def test_player_bounds_are_sensible():
    assert MIN_PLAYERS == 3
    assert MAX_PLAYERS == 16
    assert MIN_PLAYERS < MAX_PLAYERS


def test_ai_prompts_are_nonempty_strings():
    assert isinstance(AI_SYSTEM_PROMPT, str) and AI_SYSTEM_PROMPT
    assert isinstance(AI_USER_PROMPT, str) and AI_USER_PROMPT


# ── clamp_config_values ──────────────────────────────────────────────


def test_clamp_config_values_within_range_unchanged():
    assert clamp_config_values(5, 120, 40) == (5, 120, 40)


def test_clamp_config_values_below_min_pulled_up():
    assert clamp_config_values(0, 5, 1) == (1, 15, 10)


def test_clamp_config_values_above_max_pushed_down():
    assert clamp_config_values(999, 9999, 9999) == (15, 180, 60)


# ── sort_scores ──────────────────────────────────────────────────────


def test_sort_scores_highest_first():
    out = sort_scores({"a": 10, "b": 50, "c": 30})
    assert out[0] == ("b", 50)
    assert out[-1] == ("a", 10)


def test_sort_scores_empty_dict_returns_empty():
    assert sort_scores({}) == []


# ── create_matchups: 3-player round-robin ────────────────────────────


def test_create_matchups_three_players_returns_round_robin():
    answers = {"1": "x", "2": "y", "3": "z"}
    pairs, bye = create_matchups(answers, rng=random.Random(0))
    assert bye is None
    assert len(pairs) == 3
    # Each player appears in exactly 2 of the 3 pairs (round-robin)
    seen: dict[str, int] = {"1": 0, "2": 0, "3": 0}
    for p in pairs:
        for pid in p["pair"]:
            seen[str(pid)] += 1
    assert all(v == 2 for v in seen.values())
    # Every pair starts with empty votes / no winner
    for p in pairs:
        assert p["votes"] == {}
        assert p["winner"] is None


# ── create_matchups: even player count, no bye ──────────────────────


def test_create_matchups_even_count_no_bye():
    answers = {str(i): f"answer{i}" for i in range(1, 5)}  # 4 players
    pairs, bye = create_matchups(answers, rng=random.Random(0))
    assert bye is None
    assert len(pairs) == 2
    # All four ids are present across the pairs
    flat = [pid for p in pairs for pid in p["pair"]]
    assert sorted(flat) == ["1", "2", "3", "4"]


# ── create_matchups: odd player count, bye logic ────────────────────


def test_create_matchups_odd_count_picks_bye():
    answers = {str(i): f"answer{i}" for i in range(1, 6)}  # 5 players
    pairs, bye = create_matchups(answers, rng=random.Random(0))
    assert bye is not None
    assert len(pairs) == 2  # 4 players paired, 1 bye
    flat = [pid for p in pairs for pid in p["pair"]]
    assert str(bye) not in flat


def test_create_matchups_odd_count_avoids_last_bye_when_possible():
    """When last_bye_id matches a player id in the answers dict, that player
    shouldn't be picked again. Note: answers keys are strings, so the cog
    passes the bye id through as the same str that appears in answer keys."""
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    last_bye = "3"  # matches the string keys in answers
    # Run several times — bye should never be "3" since other candidates exist
    for seed in range(10):
        pairs, bye = create_matchups(
            answers, last_bye_id=last_bye, rng=random.Random(seed)
        )
        assert bye != last_bye


def test_create_matchups_odd_count_falls_back_when_last_bye_not_in_players():
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    pairs, bye = create_matchups(
        answers, last_bye_id=999, rng=random.Random(0)
    )
    # last_bye not in players → falls through to "anyone goes" path.
    # answers dict keys are strings, so bye preserves that type.
    assert bye in ["1", "2", "3", "4", "5"]


# ── create_matchups: duplicate-answer avoidance ─────────────────────


def test_create_matchups_avoids_pairing_identical_answers():
    """When non-duplicate pairings exist, duplicates shouldn't be paired."""
    # 4 players: 1 and 2 share an answer; 3 and 4 are different.
    answers = {"1": "same", "2": "same", "3": "alt", "4": "other"}
    found_dup_avoidance = False
    for seed in range(20):
        pairs, _ = create_matchups(answers, rng=random.Random(seed))
        for p in pairs:
            a, b = str(p["pair"][0]), str(p["pair"][1])
            if answers[a].strip().lower() == answers[b].strip().lower():
                break
        else:
            found_dup_avoidance = True
            break
    assert found_dup_avoidance, "Should find a no-duplicate pairing in 20 tries"


def test_create_matchups_all_identical_answers_force_pairs_anyway():
    """When every player gave the same answer, we still pair them up."""
    answers = {"1": "same", "2": "same", "3": "same", "4": "same"}
    pairs, _ = create_matchups(answers, rng=random.Random(0))
    assert len(pairs) == 2
    # Every pair will have identical answers — that's expected
    for p in pairs:
        assert len(p["pair"]) == 2


def test_create_matchups_strips_and_lowercases_for_dup_check():
    """Duplicate detection ignores surrounding whitespace and case."""
    answers = {"1": "  HELLO ", "2": "hello", "3": "different", "4": "other"}
    # Run a bunch of seeds — 1 and 2 should rarely (ideally never) end up paired
    paired_dups = 0
    for seed in range(20):
        pairs, _ = create_matchups(answers, rng=random.Random(seed))
        for p in pairs:
            a, b = str(p["pair"][0]), str(p["pair"][1])
            if {a, b} == {"1", "2"}:
                paired_dups += 1
    # At least some seeds should find the non-dup pairing
    assert paired_dups < 20


# ── calculate_matchup_score ─────────────────────────────────────────


def test_calculate_matchup_score_no_votes_returns_5050_tie():
    result = calculate_matchup_score({}, 10, 20)
    assert result["winner"] is None
    assert result["scores"] == {10: 50, 20: 50}
    assert result["clapback"] is False
    assert result["vote_counts"] == {10: 0, 20: 0}


def test_calculate_matchup_score_single_vote_no_clapback():
    """Clapback rule requires >= 2 votes even when unanimous."""
    result = calculate_matchup_score({"v1": 10}, 10, 20)
    assert result["clapback"] is False
    assert result["winner"] == 10


def test_calculate_matchup_score_two_unanimous_is_clapback():
    result = calculate_matchup_score({"v1": 10, "v2": 10}, 10, 20)
    assert result["clapback"] is True
    assert result["winner"] == 10
    # +25 clapback bonus on top of 100% pct
    assert result["scores"][10] == 125
    assert result["scores"][20] == 0


def test_calculate_matchup_score_split_no_clapback():
    """Non-unanimous → no clapback even if a clear winner."""
    result = calculate_matchup_score(
        {"v1": 10, "v2": 10, "v3": 20}, 10, 20
    )
    assert result["clapback"] is False
    assert result["winner"] == 10
    assert result["vote_counts"] == {10: 2, 20: 1}


def test_calculate_matchup_score_even_split_is_tie():
    result = calculate_matchup_score(
        {"v1": 10, "v2": 20}, 10, 20
    )
    assert result["winner"] is None
    assert result["scores"][10] == 50
    assert result["scores"][20] == 50


def test_calculate_matchup_score_player_b_wins():
    result = calculate_matchup_score(
        {"v1": 20, "v2": 20, "v3": 10}, 10, 20
    )
    assert result["winner"] == 20
    assert result["vote_counts"] == {10: 1, 20: 2}


def test_calculate_matchup_score_unanimous_b_yields_clapback():
    result = calculate_matchup_score(
        {"v1": 20, "v2": 20}, 10, 20
    )
    assert result["clapback"] is True
    assert result["winner"] == 20
    assert result["scores"][20] == 125
    assert result["scores"][10] == 0


def test_calculate_matchup_score_handles_string_or_int_vote_values():
    """Votes can come in either str or int form — the score should match."""
    int_votes = {"v1": 10, "v2": 10}
    str_votes = {"v1": "10", "v2": "10"}
    assert (
        calculate_matchup_score(int_votes, 10, 20)["winner"]
        == calculate_matchup_score(str_votes, 10, 20)["winner"]
    )


# ── find_best_answer_record ─────────────────────────────────────────


def test_find_best_answer_record_returns_none_for_no_history():
    assert find_best_answer_record([]) is None


def test_find_best_answer_record_skips_matchups_with_under_3_votes():
    history = [
        {
            "round": 1,
            "matchups": [
                {"player_a": 1, "answer_a": "a1", "votes_a": 2,
                 "player_b": 2, "answer_b": "a2", "votes_b": 0,
                 "clapback": False},
            ],
        }
    ]
    assert find_best_answer_record(history) is None


def test_find_best_answer_record_picks_highest_pct():
    history = [
        {
            "round": 1,
            "matchups": [
                # 3-0: 100% for player 1
                {"player_a": 1, "answer_a": "best", "votes_a": 3,
                 "player_b": 2, "answer_b": "loser", "votes_b": 0,
                 "clapback": True},
                # 3-2: 60/40 split
                {"player_a": 3, "answer_a": "ok", "votes_a": 3,
                 "player_b": 4, "answer_b": "less", "votes_b": 2,
                 "clapback": False},
            ],
        }
    ]
    rec = find_best_answer_record(history)
    assert rec is not None
    assert rec["text"] == "best"
    assert rec["author"] == 1
    assert rec["pct"] == 1.0
    assert rec["round"] == 1


def test_find_best_answer_record_tiebreaks_by_more_votes():
    """When two answers have equal pct, the one with more raw votes wins."""
    history = [
        {
            "round": 1,
            "matchups": [
                # 3-0 → 100%
                {"player_a": 1, "answer_a": "small_win", "votes_a": 3,
                 "player_b": 2, "answer_b": "lost", "votes_b": 0,
                 "clapback": True},
            ],
        },
        {
            "round": 2,
            "matchups": [
                # 5-0 → 100% — same pct, more votes
                {"player_a": 3, "answer_a": "big_win", "votes_a": 5,
                 "player_b": 4, "answer_b": "lost", "votes_b": 0,
                 "clapback": True},
            ],
        },
    ]
    rec = find_best_answer_record(history)
    assert rec is not None
    assert rec["text"] == "big_win"
    assert rec["round"] == 2


def test_find_best_answer_record_finds_b_side_if_better():
    history = [
        {
            "round": 1,
            "matchups": [
                {"player_a": 1, "answer_a": "loser", "votes_a": 0,
                 "player_b": 2, "answer_b": "winner_b", "votes_b": 3,
                 "clapback": True},
            ],
        }
    ]
    rec = find_best_answer_record(history)
    assert rec is not None
    assert rec["text"] == "winner_b"
    assert rec["author"] == 2


# ── find_closest_matchup_record ─────────────────────────────────────


def test_find_closest_matchup_record_returns_none_for_no_history():
    assert find_closest_matchup_record([]) is None


def test_find_closest_matchup_record_skips_zero_vote_matchups():
    history = [
        {
            "round": 1,
            "matchups": [
                {"player_a": 1, "answer_a": "x", "votes_a": 0,
                 "player_b": 2, "answer_b": "y", "votes_b": 0,
                 "clapback": False},
            ],
        }
    ]
    assert find_closest_matchup_record(history) is None


def test_find_closest_matchup_record_picks_smallest_margin():
    history = [
        {
            "round": 1,
            "matchups": [
                # Margin 3
                {"player_a": 1, "answer_a": "a1", "votes_a": 5,
                 "player_b": 2, "answer_b": "a2", "votes_b": 2,
                 "clapback": False},
                # Margin 1 — tighter
                {"player_a": 3, "answer_a": "a3", "votes_a": 3,
                 "player_b": 4, "answer_b": "a4", "votes_b": 2,
                 "clapback": False},
            ],
        }
    ]
    rec = find_closest_matchup_record(history)
    assert rec is not None
    assert rec["matchup"]["answer_a"] == "a3"
    assert rec["round"] == 1


def test_find_closest_matchup_record_tiebreaks_by_higher_total():
    """Same margin? prefer the one with more total votes."""
    history = [
        {
            "round": 1,
            "matchups": [
                # Margin 1, total 3
                {"player_a": 1, "answer_a": "small", "votes_a": 2,
                 "player_b": 2, "answer_b": "small_l", "votes_b": 1,
                 "clapback": False},
            ],
        },
        {
            "round": 2,
            "matchups": [
                # Margin 1, total 9 — wins the tiebreak
                {"player_a": 3, "answer_a": "big", "votes_a": 5,
                 "player_b": 4, "answer_b": "big_l", "votes_b": 4,
                 "clapback": False},
            ],
        },
    ]
    rec = find_closest_matchup_record(history)
    assert rec is not None
    assert rec["matchup"]["answer_a"] == "big"
    assert rec["round"] == 2


# ── shuffled_replay_config ──────────────────────────────────────────


def test_shuffled_replay_config_changes_three_fields():
    base = {"rounds": 5, "timer": 120, "vote_timer": 40, "source": "both", "anonymous": False}
    new_cfg = shuffled_replay_config(base, rng=random.Random(42))
    assert new_cfg["rounds"] in range(3, 9)
    assert new_cfg["timer"] in {60, 90, 120, 150, 180}
    assert new_cfg["vote_timer"] in {30, 40, 50, 60}
    # Other fields preserved
    assert new_cfg["source"] == "both"
    assert new_cfg["anonymous"] is False


def test_shuffled_replay_config_does_not_mutate_base():
    base = {"rounds": 5, "timer": 120, "vote_timer": 40}
    new_cfg = shuffled_replay_config(base, rng=random.Random(0))
    assert base == {"rounds": 5, "timer": 120, "vote_timer": 40}
    assert new_cfg is not base


def test_shuffled_replay_config_deterministic_with_pinned_rng():
    base = {"rounds": 5, "timer": 120, "vote_timer": 40}
    a = shuffled_replay_config(base, rng=random.Random(42))
    b = shuffled_replay_config(base, rng=random.Random(42))
    assert a == b


# ── build_lobby_embed ───────────────────────────────────────────────


def test_build_lobby_embed_empty_players_shows_nobody():
    cfg = {"rounds": 5}
    embed = build_lobby_embed("Alice", cfg, [], _name_resolver)
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Players (0)"] == "(nobody yet)"


def test_build_lobby_embed_shows_host_and_round_count_in_description():
    cfg = {"rounds": 7}
    embed = build_lobby_embed("Alice", cfg, [], _name_resolver)
    desc = embed.description or ""
    assert "Alice" in desc
    assert "7 rounds" in desc


def test_build_lobby_embed_with_under_ten_lists_all_players():
    cfg = {"rounds": 5}
    embed = build_lobby_embed("Alice", cfg, [1, 2, 3], _name_resolver)
    by_name = {f.name: f.value for f in embed.fields}
    field_value = by_name["Players (3)"]
    assert field_value is not None
    assert "User1" in field_value
    assert "User2" in field_value
    assert "User3" in field_value


def test_build_lobby_embed_with_over_ten_truncates_with_more_suffix():
    cfg = {"rounds": 5}
    players = list(range(1, 13))  # 12 players
    embed = build_lobby_embed("Alice", cfg, players, _name_resolver)
    by_name = {f.name: f.value for f in embed.fields}
    field_value = by_name["Players (12)"]
    assert field_value is not None
    assert "(+2 more)" in field_value
    assert "User1" in field_value


# ── build_submit_embed ──────────────────────────────────────────────


def test_build_submit_embed_renders_prompt_and_counts():
    embed = build_submit_embed(
        prompt="A weird prompt",
        round_num=2,
        total_rounds=5,
        deadline_str="<t:123:R>",
        answers_in=1,
        total_players=4,
    )
    assert embed.title is not None
    assert "Round 2/5" in embed.title
    assert embed.description is not None and "A weird prompt" in embed.description
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Timer"] == "<t:123:R>"
    assert by_name["Answers In"] == "1/4"


# ── build_vote_embed ────────────────────────────────────────────────


def test_build_vote_embed_renders_both_answers_and_matchup_progress():
    embed = build_vote_embed(
        answer_a="ans A",
        answer_b="ans B",
        round_num=1,
        matchup_index=0,
        total_matchups=2,
        deadline_str="<t:99:R>",
        vote_count=0,
    )
    assert embed.title is not None
    assert "Round 1" in embed.title
    assert "Matchup 1/2" in embed.title
    assert embed.description is not None
    assert "ans A" in embed.description
    assert "ans B" in embed.description


def test_build_vote_embed_escapes_markdown_in_answers():
    embed = build_vote_embed(
        answer_a="**bold**",
        answer_b="_italic_",
        round_num=1,
        matchup_index=0,
        total_matchups=1,
        deadline_str="<t:1:R>",
    )
    assert embed.description is not None
    assert "\\*\\*bold\\*\\*" in embed.description
    assert "\\_italic\\_" in embed.description


# ── build_reveal_embed: clapback branch ─────────────────────────────


def test_build_reveal_embed_clapback_branch():
    result = {
        "winner": 10,
        "scores": {10: 125, 20: 0},
        "clapback": True,
        "vote_counts": {10: 3, 20: 0},
    }
    embed = build_reveal_embed(
        result=result,
        answers={"10": "winning answer", "20": "losing answer"},
        player_a=10,
        player_b=20,
        anonymous=False,
        name_resolver=_name_resolver,
    )
    assert embed.title is not None
    assert "C L A P B A C K" in embed.title
    field_names = [f.name for f in embed.fields]
    assert "🏆 Winner" in field_names
    assert "💀 Defeated" in field_names
    # Winner field mentions the winner's name and +pts
    winner_field = next(f for f in embed.fields if f.name == "🏆 Winner")
    assert winner_field.value is not None
    assert "User10" in winner_field.value
    assert "+125" in winner_field.value


def test_build_reveal_embed_clapback_anonymous_hides_names():
    result = {
        "winner": 10,
        "scores": {10: 125, 20: 0},
        "clapback": True,
        "vote_counts": {10: 2, 20: 0},
    }
    embed = build_reveal_embed(
        result=result,
        answers={"10": "win", "20": "lose"},
        player_a=10,
        player_b=20,
        anonymous=True,
        name_resolver=_name_resolver,
    )
    winner_field = next(f for f in embed.fields if f.name == "🏆 Winner")
    assert winner_field.value is not None
    assert "User10" not in winner_field.value
    assert "???" in winner_field.value


def test_build_reveal_embed_clapback_winner_is_player_b():
    """When player_b is the clapback winner, the loser is player_a."""
    result = {
        "winner": 20,
        "scores": {10: 0, 20: 125},
        "clapback": True,
        "vote_counts": {10: 0, 20: 2},
    }
    embed = build_reveal_embed(
        result=result,
        answers={"10": "loser side", "20": "winner side"},
        player_a=10,
        player_b=20,
        anonymous=False,
        name_resolver=_name_resolver,
    )
    winner_field = next(f for f in embed.fields if f.name == "🏆 Winner")
    assert winner_field.value is not None
    assert "winner side" in winner_field.value
    assert "User20" in winner_field.value


# ── build_reveal_embed: tie branch ──────────────────────────────────


def test_build_reveal_embed_tie_branch():
    result = {
        "winner": None,
        "scores": {10: 50, 20: 50},
        "clapback": False,
        "vote_counts": {10: 1, 20: 1},
    }
    embed = build_reveal_embed(
        result=result,
        answers={"10": "answer A", "20": "answer B"},
        player_a=10,
        player_b=20,
        anonymous=False,
        name_resolver=_name_resolver,
    )
    assert embed.title is not None
    assert "TIE" in embed.title
    # Tie field shows both answers + names
    tie_field = next(f for f in embed.fields if f.name == "🤝")
    assert tie_field.value is not None
    assert "answer A" in tie_field.value
    assert "answer B" in tie_field.value
    assert "User10" in tie_field.value
    assert "User20" in tie_field.value


# ── build_reveal_embed: regular-win branch ──────────────────────────


def test_build_reveal_embed_regular_win_branch():
    result = {
        "winner": 10,
        "scores": {10: 67, 20: 33},
        "clapback": False,
        "vote_counts": {10: 2, 20: 1},
    }
    embed = build_reveal_embed(
        result=result,
        answers={"10": "winner answer", "20": "loser answer"},
        player_a=10,
        player_b=20,
        anonymous=False,
        name_resolver=_name_resolver,
    )
    assert embed.title is not None
    assert "MATCHUP RESULT" in embed.title
    # No "C L A P B A C K" prefix
    assert "C L A P B A C K" not in embed.title
    winner_field = next(f for f in embed.fields if f.name == "🏆 Winner")
    assert winner_field.value is not None
    assert "winner answer" in winner_field.value
    assert "User10" in winner_field.value


# ── build_scoreboard_embed ──────────────────────────────────────────


def test_build_scoreboard_embed_no_bye_omits_bye_field():
    payload = {"scores": {"1": 100, "2": 50}}
    embed = build_scoreboard_embed(payload, 1, 5, bye_player=None)
    field_names = [f.name for f in embed.fields]
    assert "📊 Scoreboard" in field_names
    assert "Bye" not in field_names


def test_build_scoreboard_embed_with_bye_includes_bye_field():
    payload = {"scores": {"1": 100, "2": 50, "3": 0}}
    embed = build_scoreboard_embed(payload, 1, 5, bye_player=3)
    field_names = [f.name for f in embed.fields]
    assert "Bye" in field_names
    bye_field = next(f for f in embed.fields if f.name == "Bye")
    assert bye_field.value is not None
    assert "<@3>" in bye_field.value


def test_build_scoreboard_embed_sorts_scores_highest_first():
    payload = {"scores": {"1": 30, "2": 100, "3": 50}}
    embed = build_scoreboard_embed(payload, 2, 5, bye_player=None)
    sb_field = next(f for f in embed.fields if f.name == "📊 Scoreboard")
    assert sb_field.value is not None
    # Player 2 (100) should appear before player 3 (50) before player 1 (30)
    lines = sb_field.value.splitlines()
    assert "<@2>" in lines[0]
    assert "<@3>" in lines[1]
    assert "<@1>" in lines[2]


def test_build_scoreboard_embed_final_round_uses_no_remaining_text():
    payload = {"scores": {"1": 10}}
    embed = build_scoreboard_embed(payload, 5, 5, bye_player=None)
    last_field = embed.fields[-1]
    assert last_field.value is not None
    assert "Final round" in last_field.value


def test_build_scoreboard_embed_with_remaining_rounds_shows_count():
    payload = {"scores": {"1": 10}}
    embed = build_scoreboard_embed(payload, 2, 5, bye_player=None)
    last_field = embed.fields[-1]
    assert last_field.value is not None
    assert "3 round(s) remaining" in last_field.value


def test_build_scoreboard_embed_empty_scores_shows_placeholder():
    embed = build_scoreboard_embed({"scores": {}}, 1, 5, bye_player=None)
    sb_field = next(f for f in embed.fields if f.name == "📊 Scoreboard")
    assert sb_field.value == "No scores yet"


# ── build_recap_embed ───────────────────────────────────────────────


def test_build_recap_embed_with_no_scores_uses_nobody_placeholder():
    payload = {"scores": {}, "clapbacks": {}, "round_history": [], "players": []}
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    # Winner field title contains "Nobody" in the heading
    field_names = [f.name for f in embed.fields if f.name]
    assert any("Nobody" in n for n in field_names)


def test_build_recap_embed_winner_field_uses_highest_scorer():
    payload = {
        "scores": {"10": 250, "20": 100, "30": 50},
        "clapbacks": {"10": 2, "20": 0, "30": 0},
        "round_history": [],
        "players": [10, 20, 30],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    winner_field_name = next(
        n for n in (f.name for f in embed.fields) if n and "WINNER" in n
    )
    assert "User10" in winner_field_name


def test_build_recap_embed_scoreboard_includes_clapback_counts():
    payload = {
        "scores": {"10": 100, "20": 50},
        "clapbacks": {"10": 2, "20": 0},
        "round_history": [],
        "players": [10, 20],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    sb_field = next(f for f in embed.fields if f.name == "📊 Final Scoreboard")
    assert sb_field.value is not None
    # Player 10 has 2 clapbacks
    assert "2 CLAPBACKS" in sb_field.value
    # Player 20 has none — no CLAPBACK suffix
    lines = sb_field.value.splitlines()
    player20_line = next(line for line in lines if "User20" in line)
    assert "CLAPBACK" not in player20_line


def test_build_recap_embed_singular_clapback_suffix():
    """One clapback uses 'CLAPBACK' (no S), two+ use 'CLAPBACKS'."""
    payload = {
        "scores": {"10": 100},
        "clapbacks": {"10": 1},
        "round_history": [],
        "players": [10],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    sb_field = next(f for f in embed.fields if f.name == "📊 Final Scoreboard")
    assert sb_field.value is not None
    assert "1 CLAPBACK)" in sb_field.value
    assert "CLAPBACKS" not in sb_field.value


def test_build_recap_embed_includes_best_answer_when_qualifying():
    payload = {
        "scores": {"10": 100, "20": 50},
        "clapbacks": {"10": 1, "20": 0},
        "round_history": [
            {
                "round": 1,
                "matchups": [
                    {"player_a": 10, "answer_a": "best!", "votes_a": 3,
                     "player_b": 20, "answer_b": "ok", "votes_b": 0,
                     "clapback": True},
                ],
            }
        ],
        "players": [10, 20],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    field_names = [f.name for f in embed.fields]
    assert "⚡ Best Single Answer" in field_names
    best_field = next(f for f in embed.fields if f.name == "⚡ Best Single Answer")
    assert best_field.value is not None
    assert "best!" in best_field.value
    assert "User10" in best_field.value


def test_build_recap_embed_best_answer_anonymous_hides_author():
    payload = {
        "scores": {"10": 100},
        "clapbacks": {"10": 1},
        "round_history": [
            {
                "round": 1,
                "matchups": [
                    {"player_a": 10, "answer_a": "best!", "votes_a": 3,
                     "player_b": 20, "answer_b": "ok", "votes_b": 0,
                     "clapback": True},
                ],
            }
        ],
        "players": [10, 20],
    }
    embed = build_recap_embed(payload, {"anonymous": True}, _name_resolver)
    best_field = next(f for f in embed.fields if f.name == "⚡ Best Single Answer")
    assert best_field.value is not None
    assert "User10" not in best_field.value
    assert "???" in best_field.value


def test_build_recap_embed_includes_closest_matchup_when_qualifying():
    payload = {
        "scores": {"10": 100, "20": 100},
        "clapbacks": {"10": 0, "20": 0},
        "round_history": [
            {
                "round": 1,
                "matchups": [
                    {"player_a": 10, "answer_a": "a", "votes_a": 1,
                     "player_b": 20, "answer_b": "b", "votes_b": 1,
                     "clapback": False},
                ],
            }
        ],
        "players": [10, 20],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    field_names = [f.name for f in embed.fields]
    assert "🤣 Closest Matchup" in field_names


def test_build_recap_embed_includes_total_clapbacks_when_nonzero():
    payload = {
        "scores": {"10": 100, "20": 50},
        "clapbacks": {"10": 2, "20": 1},
        "round_history": [],
        "players": [10, 20],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    field_names = [f.name for f in embed.fields]
    assert "⚡ Total CLAPBACKS" in field_names
    total_field = next(f for f in embed.fields if f.name == "⚡ Total CLAPBACKS")
    assert total_field.value == "3"


def test_build_recap_embed_omits_total_clapbacks_when_zero():
    payload = {
        "scores": {"10": 100, "20": 50},
        "clapbacks": {"10": 0, "20": 0},
        "round_history": [],
        "players": [10, 20],
    }
    embed = build_recap_embed(payload, {"anonymous": False}, _name_resolver)
    field_names = [f.name for f in embed.fields]
    assert "⚡ Total CLAPBACKS" not in field_names


@pytest.mark.parametrize("rounds,timer,vote_timer", [
    (5, 120, 40),
    (1, 15, 10),
    (15, 180, 60),
])
def test_clamp_config_values_boundary_inputs_unchanged(rounds, timer, vote_timer):
    """Boundary-valid inputs aren't altered."""
    assert clamp_config_values(rounds, timer, vote_timer) == (rounds, timer, vote_timer)
