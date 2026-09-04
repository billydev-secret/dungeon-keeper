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

import discord
import pytest

from bot_modules.games.constants import GAME_ICONS
from bot_modules.games_clapback.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
    build_scoreboard_embed,
    build_submit_embed,
    build_vote_embed,
)
from bot_modules.services.embeds import COLOR_GREEN
from bot_modules.games_clapback.logic import (
    AI_SYSTEM_PROMPT,
    AI_USER_PROMPT,
    MAX_PLAYERS,
    MIN_PLAYERS,
    admit_pending_players,
    admit_player_now,
    calculate_bye_award,
    calculate_matchup_score,
    drain_pending_players,
    clamp_config_values,
    create_matchups,
    find_best_answer_record,
    find_closest_matchup_record,
    pick_round_bye,
    shuffled_replay_config,
    sort_scores,
    vote_button_label,
)
from bot_modules.core.branding import SECTION_SPACER


def _unspaced(value: str | None) -> str:
    """A field value without the trailing spacer ``apply_section_spacing`` adds.

    Every field but the last carries ``SECTION_SPACER`` for breathing room
    (docs/embed_style_guide.md § Section spacing). These tests assert content,
    not spacing, so they compare against the value with it removed.
    """
    text = value or ""
    return text[: -len(SECTION_SPACER)] if text.endswith(SECTION_SPACER) else text



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


def test_create_matchups_odd_count_skips_players_who_already_had_a_bye():
    """Anyone already in bye_history is passed over while a player with
    zero byes is still available. Note: answers keys are strings, so the
    cog passes bye ids through as the same str the answer keys use."""
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    for seed in range(10):
        _, bye = create_matchups(
            answers, bye_history=["3"], rng=random.Random(seed)
        )
        assert bye != "3"


def test_create_matchups_odd_count_ignores_bye_history_for_absent_players():
    """A bye id that isn't among this round's submitters constrains
    nothing — everyone present is equally overdue."""
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    _, bye = create_matchups(answers, bye_history=[999], rng=random.Random(0))
    assert bye in ["1", "2", "3", "4", "5"]


def test_create_matchups_everyone_byes_once_before_anyone_byes_twice():
    """The whole point of the rotation: across five odd rounds with a
    stable roster, five distinct players sit out — no repeats."""
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    history: list[str] = []
    for round_num in range(5):
        _, bye = create_matchups(
            answers, bye_history=history, rng=random.Random(round_num)
        )
        history.append(bye)
    assert sorted(history) == ["1", "2", "3", "4", "5"]


def test_create_matchups_second_lap_reuses_players_with_fewest_byes():
    """Once everyone has one bye, round six starts a fresh lap rather
    than deadlocking or favouring whoever sat out first."""
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    history = ["1", "2", "3", "4", "5", "1", "2"]
    # 3, 4 and 5 are tied on one bye each; 1 and 2 have two.
    for seed in range(10):
        _, bye = create_matchups(
            answers, bye_history=history, rng=random.Random(seed)
        )
        assert bye in ["3", "4", "5"]


def test_create_matchups_rotation_survives_a_changing_submitter_set():
    """Counting byes (rather than remembering only the last one) is what
    makes the rule hold when someone misses the submit window: player 5
    skips round two, and still doesn't get a second bye in round three."""
    full = {str(i): f"answer{i}" for i in range(1, 6)}
    without_two = {k: v for k, v in full.items() if k != "2"}

    history: list[str] = []
    _, bye1 = create_matchups(full, bye_history=history, rng=random.Random(1))
    history.append(bye1)
    # Round two has an even count without player 2's answer, so no bye.
    _, bye2 = create_matchups(without_two, bye_history=history, rng=random.Random(2))
    assert bye2 is None
    _, bye3 = create_matchups(full, bye_history=history, rng=random.Random(3))
    assert bye3 != bye1


def test_create_matchups_no_bye_history_treats_everyone_equally():
    answers = {str(i): f"answer{i}" for i in range(1, 6)}
    seen = {create_matchups(answers, rng=random.Random(s))[1] for s in range(25)}
    # With nobody constrained, the bye shouldn't be pinned to one player.
    assert len(seen) > 1


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


@pytest.mark.parametrize("seed", range(30))
def test_create_matchups_never_drops_a_player_when_dupes_are_unavoidable(seed):
    """Regression: four identical answers among six players means every
    shuffle collides. The retry loop used to keep the *partial* pair list
    built before it broke out, so four of six players silently vanished
    from the round. Every submitter must be pairable or the bye."""
    answers = {
        "1": "same", "2": "same", "3": "same",
        "4": "same", "5": "x", "6": "y",
    }
    pairs, bye = create_matchups(answers, rng=random.Random(seed))
    assert bye is None  # even count
    flat = sorted(str(pid) for p in pairs for pid in p["pair"])
    assert flat == ["1", "2", "3", "4", "5", "6"]


@pytest.mark.parametrize("seed", range(20))
def test_create_matchups_odd_count_pairs_everyone_but_the_bye(seed):
    """Same guarantee with a bye in play: 7 submitters → 3 matchups
    covering the 6 non-bye players, even with unavoidable duplicates."""
    answers = {str(i): "same" for i in range(1, 6)} | {"6": "x", "7": "y"}
    pairs, bye = create_matchups(answers, rng=random.Random(seed))
    flat = sorted(str(pid) for p in pairs for pid in p["pair"])
    assert len(flat) == 6
    assert str(bye) not in flat
    assert sorted(flat + [str(bye)]) == [str(i) for i in range(1, 8)]


def test_create_matchups_minimises_duplicate_pairs_it_cannot_avoid():
    """When some duplication is forced, the chosen pairing should carry
    the fewest same-answer matchups, not merely the first one tried."""
    answers = {
        "1": "same", "2": "same", "3": "same",
        "4": "same", "5": "x", "6": "y",
    }
    for seed in range(20):
        pairs, _ = create_matchups(answers, rng=random.Random(seed))
        dupes = sum(
            1 for p in pairs
            if answers[str(p["pair"][0])] == answers[str(p["pair"][1])]
        )
        # Best possible here is 1: pair two "same" players together and
        # spend the other two against x and y.
        assert dupes == 1


# ── calculate_bye_award ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "round_points,expected",
    [
        pytest.param([60, 40, 75, 25], 50, id="balanced-field-averages-50"),
        pytest.param([100, 0], 50, id="two-player-blowout"),
        pytest.param([125, 0, 80, 20], 56, id="clapback-bonus-lifts-the-mean"),
        pytest.param([33, 67, 50], 50, id="rounds-to-nearest-int"),
        pytest.param([70, 30, 70, 30, 60], 52, id="odd-count-rounds-down"),
        pytest.param([], 50, id="no-matchups-falls-back-to-default"),
        pytest.param(None, 50, id="none-falls-back-to-default"),
    ],
)
def test_calculate_bye_award(round_points, expected):
    assert calculate_bye_award(round_points) == expected


def test_calculate_bye_award_honours_a_custom_default():
    assert calculate_bye_award([], default=0) == 0


def test_calculate_bye_award_tracks_a_high_scoring_round():
    """A round where everyone did well pays the bye player more than a
    round where everyone bombed — that's the 'neither punish nor
    reward' property the flat 50 didn't have."""
    assert calculate_bye_award([90, 85, 100, 80]) > calculate_bye_award([20, 15, 30, 10])


# ── calculate_matchup_score ─────────────────────────────────────────


@pytest.mark.parametrize(
    "votes,expected",
    [
        # The 50/50 zero-votes fallback is the cog's intentional
        # "show up and play" behavior.
        pytest.param(
            {},
            {"winner": None, "scores": {10: 50, 20: 50},
             "clapback": False, "vote_counts": {10: 0, 20: 0}},
            id="no-votes-5050-tie",
        ),
        # Clapback rule requires >= 2 votes even when unanimous.
        pytest.param(
            {"v1": 10},
            {"winner": 10, "scores": {10: 100, 20: 0},
             "clapback": False, "vote_counts": {10: 1, 20: 0}},
            id="single-vote-no-clapback",
        ),
        # +25 clapback bonus on top of 100% pct
        pytest.param(
            {"v1": 10, "v2": 10},
            {"winner": 10, "scores": {10: 125, 20: 0},
             "clapback": True, "vote_counts": {10: 2, 20: 0}},
            id="two-unanimous-is-clapback",
        ),
        # Non-unanimous → no clapback even if a clear winner.
        pytest.param(
            {"v1": 10, "v2": 10, "v3": 20},
            {"winner": 10, "scores": {10: 67, 20: 33},
             "clapback": False, "vote_counts": {10: 2, 20: 1}},
            id="split-no-clapback",
        ),
        pytest.param(
            {"v1": 10, "v2": 20},
            {"winner": None, "scores": {10: 50, 20: 50},
             "clapback": False, "vote_counts": {10: 1, 20: 1}},
            id="even-split-is-tie",
        ),
        pytest.param(
            {"v1": 20, "v2": 20, "v3": 10},
            {"winner": 20, "scores": {10: 33, 20: 67},
             "clapback": False, "vote_counts": {10: 1, 20: 2}},
            id="player-b-wins",
        ),
        pytest.param(
            {"v1": 20, "v2": 20},
            {"winner": 20, "scores": {10: 0, 20: 125},
             "clapback": True, "vote_counts": {10: 0, 20: 2}},
            id="unanimous-b-yields-clapback",
        ),
    ],
)
def test_calculate_matchup_score(votes, expected):
    assert calculate_matchup_score(votes, 10, 20) == expected


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
    by_name = {f.name: _unspaced(f.value) for f in embed.fields}
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
    by_name = {f.name: _unspaced(f.value) for f in embed.fields}
    field_value = by_name["Players (3)"]
    assert field_value is not None
    assert "User1" in field_value
    assert "User2" in field_value
    assert "User3" in field_value


def test_build_lobby_embed_with_over_ten_truncates_with_more_suffix():
    cfg = {"rounds": 5}
    players = list(range(1, 13))  # 12 players
    embed = build_lobby_embed("Alice", cfg, players, _name_resolver)
    by_name = {f.name: _unspaced(f.value) for f in embed.fields}
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
    by_name = {f.name: _unspaced(f.value) for f in embed.fields}
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


def test_build_vote_embed_shows_prompt_when_supplied():
    embed = build_vote_embed(
        answer_a="ans A",
        answer_b="ans B",
        round_num=1,
        matchup_index=0,
        total_matchups=1,
        deadline_str="<t:1:R>",
        prompt="A terrible name for a pet store",
    )
    assert embed.description is not None
    assert "A terrible name for a pet store" in embed.description


def test_build_vote_embed_omits_prompt_when_absent():
    embed = build_vote_embed(
        answer_a="ans A",
        answer_b="ans B",
        round_num=1,
        matchup_index=0,
        total_matchups=1,
        deadline_str="<t:1:R>",
    )
    assert embed.description is not None
    assert "💬" not in embed.description


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
    assert "Clapback" in embed.title
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
    assert "Tie" in embed.title
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
    assert "Matchup Result" in embed.title
    # No "C L A P B A C K" prefix
    assert "Clapback" not in embed.title


def test_build_reveal_embed_shows_prompt_when_supplied():
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
        prompt="The worst superpower to have on a first date",
    )
    assert embed.description is not None
    assert "The worst superpower to have on a first date" in embed.description
    assert embed.title is not None
    assert "Clapback" not in embed.title
    winner_field = next(f for f in embed.fields if f.name == "🏆 Winner")
    assert winner_field.value is not None
    assert "winner answer" in winner_field.value
    assert "User10" in winner_field.value


# ── build_scoreboard_embed ──────────────────────────────────────────


def test_build_scoreboard_embed_no_bye_omits_bye_field():
    payload = {"scores": {"1": 100, "2": 50}}
    embed = build_scoreboard_embed(payload, 1, 5, bye_players=None)
    field_names = [f.name for f in embed.fields]
    assert "📊 Scoreboard" in field_names
    assert "Bye" not in field_names


def test_build_scoreboard_embed_with_bye_includes_bye_field():
    payload = {"scores": {"1": 100, "2": 50, "3": 0}}
    embed = build_scoreboard_embed(
        payload, 1, 5, bye_players=[3], name_resolver=_name_resolver
    )
    field_names = [f.name for f in embed.fields]
    assert "Bye" in field_names
    bye_field = next(f for f in embed.fields if f.name == "Bye")
    assert bye_field.value is not None
    # A name, never a <@id>: an embed mention renders as digits to anyone whose
    # client hasn't cached that member.
    assert "User3" in bye_field.value
    assert "<@3>" not in bye_field.value


def test_build_scoreboard_embed_bye_field_shows_the_actual_award():
    payload = {"scores": {"1": 10, "3": 62}}
    embed = build_scoreboard_embed(payload, 1, 5, bye_players=[3], bye_award=62)
    bye_field = next(f for f in embed.fields if f.name == "Bye")
    assert bye_field.value is not None
    assert "62" in bye_field.value
    assert "50" not in bye_field.value


def test_build_scoreboard_embed_bye_award_defaults_to_fifty_for_old_records():
    """Round records written before the award went dynamic have no
    bye_award — the embed still renders rather than showing None."""
    payload = {"scores": {"1": 10, "3": 50}}
    embed = build_scoreboard_embed(payload, 1, 5, bye_players=[3])
    bye_field = next(f for f in embed.fields if f.name == "Bye")
    assert bye_field.value is not None
    assert "50" in bye_field.value


def test_build_scoreboard_embed_sorts_scores_highest_first():
    payload = {"scores": {"1": 30, "2": 100, "3": 50}}
    embed = build_scoreboard_embed(
        payload, 2, 5, bye_players=None, name_resolver=_name_resolver
    )
    sb_field = next(f for f in embed.fields if f.name == "📊 Scoreboard")
    assert sb_field.value is not None
    # Player 2 (100) should appear before player 3 (50) before player 1 (30)
    lines = sb_field.value.splitlines()
    assert "User2" in lines[0]
    assert "User3" in lines[1]
    assert "User1" in lines[2]
    assert "<@" not in sb_field.value


def test_build_scoreboard_embed_default_resolver_keeps_a_mention():
    """An un-wired caller still renders (as a mention) rather than crashing;
    the AST test below is what forces the cog to wire a resolver."""
    embed = build_scoreboard_embed({"scores": {"1": 30}}, 2, 5, bye_players=[2])
    sb_field = next(f for f in embed.fields if f.name == "📊 Scoreboard")
    bye_field = next(f for f in embed.fields if f.name == "Bye")
    assert "<@1>" in (sb_field.value or "")
    assert "<@2>" in (bye_field.value or "")


def test_build_scoreboard_embed_final_round_uses_no_remaining_text():
    payload = {"scores": {"1": 10}}
    embed = build_scoreboard_embed(payload, 5, 5, bye_players=None)
    last_field = embed.fields[-1]
    assert last_field.value is not None
    assert "Final round" in last_field.value


def test_build_scoreboard_embed_with_remaining_rounds_shows_count():
    payload = {"scores": {"1": 10}}
    embed = build_scoreboard_embed(payload, 2, 5, bye_players=None)
    last_field = embed.fields[-1]
    assert last_field.value is not None
    assert "3 round(s) remaining" in last_field.value


def test_build_scoreboard_embed_empty_scores_shows_placeholder():
    embed = build_scoreboard_embed({"scores": {}}, 1, 5, bye_players=None)
    sb_field = next(f for f in embed.fields if f.name == "📊 Scoreboard")
    assert _unspaced(sb_field.value) == "No scores yet"


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
        n for n in (f.name for f in embed.fields) if n and "Winner" in n
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
    assert "⚡ Total Clapbacks" in field_names
    total_field = next(f for f in embed.fields if f.name == "⚡ Total Clapbacks")
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
    assert "⚡ Total Clapbacks" not in field_names


# ── accent-color threading (2026-07-21 ruling: games follow guild accent) ──

_ACCENT = discord.Color(0x123456)
_GREEN = discord.Color(COLOR_GREEN)


def test_reveal_clapback_winner_stays_green_ignoring_accent():
    """A clapback is a win → green stays semantic even when an accent is passed."""
    result = {
        "winner": 10, "scores": {10: 125, 20: 0},
        "clapback": True, "vote_counts": {10: 2, 20: 0},
    }
    embed = build_reveal_embed(
        result=result, answers={"10": "win", "20": "lose"},
        player_a=10, player_b=20, anonymous=False,
        name_resolver=_name_resolver, color=_ACCENT,
    )
    assert embed.color == _GREEN


def test_reveal_regular_win_stays_green_ignoring_accent():
    result = {
        "winner": 10, "scores": {10: 67, 20: 33},
        "clapback": False, "vote_counts": {10: 2, 20: 1},
    }
    embed = build_reveal_embed(
        result=result, answers={"10": "win", "20": "lose"},
        player_a=10, player_b=20, anonymous=False,
        name_resolver=_name_resolver, color=_ACCENT,
    )
    assert embed.color == _GREEN


def test_reveal_clapback_title_leads_with_game_icon_not_wordmark():
    """The clapback reveal title now leads with the ⚔️ game icon like its
    sibling cards, not the old off-pattern '⚡ C L A P B A C K ⚡' wordmark."""
    result = {
        "winner": 10, "scores": {10: 125, 20: 0},
        "clapback": True, "vote_counts": {10: 2, 20: 0},
    }
    embed = build_reveal_embed(
        result=result, answers={"10": "win", "20": "lose"},
        player_a=10, player_b=20, anonymous=False,
        name_resolver=_name_resolver,
    )
    assert embed.title is not None
    assert embed.title.startswith(GAME_ICONS["clapback"])
    assert "⚡" not in embed.title
    assert "Clapback" in embed.title


@pytest.mark.parametrize("rounds,timer,vote_timer", [
    (5, 120, 40),
    (1, 15, 10),
    (15, 180, 60),
])
def test_clamp_config_values_boundary_inputs_unchanged(rounds, timer, vote_timer):
    """Boundary-valid inputs aren't altered."""
    assert clamp_config_values(rounds, timer, vote_timer) == (rounds, timer, vote_timer)


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_clapback_cog as clapback_cog  # noqa: E402
from tests.fakes import FakeChannel  # noqa: E402


class _SpyBot:
    def __init__(self) -> None:
        self.games_db = None
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=":memory:")

    def get_cog(self, name):
        return None


async def test_post_recap_passes_players_to_end_game(monkeypatch):
    """The genuine recap site pays the joined roster, not just the host."""
    spy = AsyncMock()
    monkeypatch.setattr(clapback_cog, "end_game", spy)
    cog = clapback_cog.ClapbackCog(_SpyBot())  # type: ignore[arg-type]
    channel = FakeChannel(id=100)
    payload = {"players": [1, 2, 3], "scores": {}, "clapbacks": {},
               "round_history": [], "host_id": 1}
    await cog._post_recap("g1", channel, payload, {"anonymous": False})
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3]
    assert call.kwargs["bot"] is cog.bot


# ── pick_round_bye: benched before the prompt, not after ─────────────────────


def test_pick_round_bye_none_for_even_field():
    assert pick_round_bye(["1", "2", "3", "4"]) is None


def test_pick_round_bye_none_for_three_players():
    """Three players run a full round-robin, so nobody sits out — the rule
    has to match create_matchups or the round would lose a player."""
    assert pick_round_bye(["1", "2", "3"]) is None


def test_pick_round_bye_picks_one_for_odd_field():
    bye = pick_round_bye(["1", "2", "3", "4", "5"], rng=random.Random(0))
    assert bye in {"1", "2", "3", "4", "5"}


def test_pick_round_bye_skips_players_who_already_sat_out():
    for seed in range(10):
        bye = pick_round_bye(
            ["1", "2", "3", "4", "5"], bye_history=["3"], rng=random.Random(seed)
        )
        assert bye != "3"


def test_pick_round_bye_rotates_everyone_before_repeating():
    history: list[str] = []
    for round_num in range(5):
        bye = pick_round_bye(
            ["1", "2", "3", "4", "5"], bye_history=history, rng=random.Random(round_num)
        )
        history.append(bye)
    assert sorted(history) == ["1", "2", "3", "4", "5"]


def test_pick_round_bye_leaves_an_even_field_for_create_matchups():
    """The point of picking up front: the remaining submitters pair cleanly,
    so create_matchups hands out no second bye."""
    players = ["1", "2", "3", "4", "5"]
    bye = pick_round_bye(players, rng=random.Random(1))
    answers = {p: f"answer{p}" for p in players if p != bye}
    _, late_bye = create_matchups(answers, rng=random.Random(1))
    assert late_bye is None


def test_pick_round_bye_a_missing_submitter_still_forces_a_late_bye():
    """A player who never answers leaves an odd submitter count, so a second
    bye is real and both have to be paid."""
    players = [str(i) for i in range(1, 8)]  # 7: pre-bye leaves 6 submitters
    bye = pick_round_bye(players, rng=random.Random(1))
    answers = {p: f"answer{p}" for p in players if p != bye}
    answers.pop(next(iter(answers)))  # someone misses the window → 5 left
    _, late_bye = create_matchups(answers, rng=random.Random(1))
    assert late_bye is not None and late_bye != bye


# ── vote_button_label: the answer goes on the button ─────────────────────────


def test_vote_button_label_carries_the_answer():
    assert vote_button_label("🅰️", "pineapple on pizza") == "🅰️: pineapple on pizza"


def test_vote_button_label_flattens_newlines():
    assert "\n" not in vote_button_label("🅱️", "two\nlines")
    assert vote_button_label("🅱️", "two\nlines") == "🅱️: two lines"


def test_vote_button_label_truncates_to_fit_a_discord_button():
    label = vote_button_label("🅰️", "x" * 200)
    assert len(label) <= 80
    assert label.endswith("…")


def test_vote_button_label_falls_back_when_the_answer_is_blank():
    assert vote_button_label("🅰️", "   ") == "Vote 🅰️"
    assert vote_button_label("🅱️", "") == "Vote 🅱️"


# ── admit_pending_players: joining at a round boundary ───────────────────────


def test_admit_pending_players_adds_them_in_order():
    roster, admitted, turned_away = admit_pending_players([1, 2], [3, 4], 10)
    assert roster == [1, 2, 3, 4]
    assert admitted == [3, 4]
    assert turned_away == []


def test_admit_pending_players_ignores_someone_already_playing():
    roster, admitted, _ = admit_pending_players([1, 2], [2], 10)
    assert roster == [1, 2]
    assert admitted == []


def test_admit_pending_players_ignores_a_double_press():
    roster, admitted, _ = admit_pending_players([1], [2, 2], 10)
    assert roster == [1, 2]
    assert admitted == [2]


def test_admit_pending_players_turns_away_over_the_cap():
    """Over-cap joiners are reported, not silently dropped — the caller says
    so in channel rather than leaving someone waiting for a round that never
    includes them."""
    roster, admitted, turned_away = admit_pending_players([1, 2, 3], [4, 5], 4)
    assert roster == [1, 2, 3, 4]
    assert admitted == [4]
    assert turned_away == [5]


def test_admit_pending_players_handles_no_queue():
    roster, admitted, turned_away = admit_pending_players([1, 2], None, 10)
    assert (roster, admitted, turned_away) == ([1, 2], [], [])


# ── scoreboard renders two byes when a round produces two ────────────────────


def test_build_scoreboard_embed_renders_multiple_byes():
    payload = {"scores": {"1": 10, "2": 5}}
    embed = build_scoreboard_embed(
        payload, 1, 5, bye_players=[3, 4], bye_award=40,
        name_resolver=_name_resolver,
    )
    field = next(f for f in embed.fields if f.name == "Byes")
    assert "User3" in (field.value or "") and "User4" in (field.value or "")
    assert "<@" not in (field.value or "")
    assert "+40" in (field.value or "") and "each" in (field.value or "")


def test_build_scoreboard_embed_accepts_a_bare_bye_id():
    """Round records written before a round could produce two byes store a
    single id; the builder still has to render them."""
    embed = build_scoreboard_embed({"scores": {"1": 10}}, 1, 5, bye_players=3)
    assert any(f.name == "Bye" for f in embed.fields)


# ── submit embed names who is sitting out ────────────────────────────────────


def test_build_submit_embed_names_the_benched_player():
    embed = build_submit_embed(
        prompt="p", round_num=1, total_rounds=3, deadline_str="⏰ 60s",
        answers_in=0, total_players=4, bye_player=7,
        name_resolver=_name_resolver,
    )
    field = next(f for f in embed.fields if "Sitting out" in (f.name or ""))
    assert "User7" in (field.value or "")
    assert "<@7>" not in (field.value or "")


def test_build_submit_embed_omits_the_field_with_no_bye():
    embed = build_submit_embed(
        prompt="p", round_num=1, total_rounds=3, deadline_str="⏰ 60s",
        answers_in=0, total_players=4,
    )
    assert not any("Sitting out" in (f.name or "") for f in embed.fields)


def test_drain_pending_players_returns_and_clears_the_queue():
    """Admission only happens at a round boundary, so anyone who pressed Join
    during the final round is queued for a round that never comes. The game end
    has to clear them and say so."""
    payload = {"pending_players": [7, 8]}
    assert drain_pending_players(payload) == [7, 8]
    assert payload["pending_players"] == []


def test_drain_pending_players_is_a_no_op_on_an_empty_queue():
    payload = {"players": [1, 2]}
    assert drain_pending_players(payload) == []
    assert payload["pending_players"] == []


# ── admit_player_now: joining the round that is already open ─────────────
#
# Queueing a latecomer for the *next* round meant sitting out the one they
# were watching. Matchups are built from the answers dict after the submit
# window closes, so anyone who gets an answer in before then can simply be
# paired — there is nothing to keep them out of.


def test_admit_player_now_seats_them_in_the_open_round():
    payload = {"phase": "submitting", "players": [1, 2], "scores": {"1": 4}}

    assert admit_player_now(payload, 3, 10) == "joined"

    assert payload["players"] == [1, 2, 3]
    # Seeded on every board a scored round touches, checkpoint included —
    # otherwise a crash-resume rolls them off the scoreboard.
    assert payload["scores"]["3"] == 0
    assert payload["scores_checkpoint"]["3"] == 0
    assert payload["clapbacks"]["3"] == 0
    # An existing score is never reset by a stray press.
    assert payload["scores"]["1"] == 4


def test_admit_player_now_leaves_someone_already_playing_alone():
    payload = {"phase": "submitting", "players": [1, 2], "scores": {"2": 7}}

    assert admit_player_now(payload, 2, 10) == "already-in"

    assert payload["players"] == [1, 2]
    assert payload["scores"]["2"] == 7


def test_admit_player_now_turns_them_away_at_the_cap():
    payload = {"phase": "submitting", "players": [1, 2, 3]}

    assert admit_player_now(payload, 4, 3) == "full"

    assert payload["players"] == [1, 2, 3]


@pytest.mark.parametrize("phase", ["voting", "revealing"])
def test_admit_player_now_falls_back_to_the_queue_once_answers_close(phase):
    """Mid-vote there is nothing to write: the matchups are already set."""
    payload = {"phase": phase, "players": [1, 2]}

    assert admit_player_now(payload, 3, 10) == "queued"

    assert payload["players"] == [1, 2]
    assert payload["pending_players"] == [3]


def test_admit_player_now_recognises_a_second_press_while_queued():
    payload = {"phase": "voting", "players": [1, 2], "pending_players": [3]}

    assert admit_player_now(payload, 3, 10) == "already-queued"

    assert payload["pending_players"] == [3]


# ── every render site in the cog passes a resolver ───────────────────────────


def test_every_clapback_render_site_passes_a_resolver():
    """``build_submit_embed`` and ``build_scoreboard_embed`` default their
    resolver to ``mention``, so a render site that forgets to pass one silently
    reintroduces digits-in-the-scoreboard and no builder test above would
    notice. This walks the cog and requires every call to a name-taking builder
    to hand a resolver over."""
    import ast
    import inspect
    import pathlib

    from bot_modules.cogs import games_clapback_cog
    from bot_modules.games_clapback import embeds as clapback_embeds

    needs = {
        name
        for name, fn in inspect.getmembers(clapback_embeds, inspect.isfunction)
        if "name_resolver" in inspect.signature(fn).parameters
    }
    assert {"build_submit_embed", "build_scoreboard_embed"} <= needs
    # Explicit utf-8: the CI runner is Windows, where the default is cp1252.
    source = pathlib.Path(inspect.getfile(games_clapback_cog)).read_text(
        encoding="utf-8"
    )
    missed = [
        f"games_clapback_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_resolver" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_resolver: " + ", ".join(missed)
