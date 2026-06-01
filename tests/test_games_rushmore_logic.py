"""Tests for the extracted Mt. Rushmore Draft pure-logic modules.

Covers ``bot_modules/games_rushmore/logic.py`` (snake-order generation,
duplicate detection, eligible voter filtering, vote tallying, recap
stats, settings clamp) and ``bot_modules/games_rushmore/embeds.py``
(join, draft, final-boards, vote, winner, recap embed builders, plus
the ``render_draft_board`` string helper).

Mirrors the games_ttl / games_clapback pattern: the cog file stays
thin; this module proves the extracted pieces work without spinning up
Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games_rushmore.embeds import (
    build_draft_embed,
    build_final_boards_embed,
    build_join_embed,
    build_recap_embed,
    build_vote_embed,
    build_winner_embed,
    render_draft_board,
)
from bot_modules.games_rushmore.logic import (
    DRAFT_ROUNDS,
    SKIPPED_MARKER,
    clamp_settings,
    compute_recap_stats,
    eligible_voters,
    find_who_picked,
    generate_snake_order,
    is_duplicate,
    tally_votes,
)


def _name_resolver(uid: int) -> str:
    return f"User{uid}"


# ── constants ────────────────────────────────────────────────────────


def test_draft_rounds_is_four():
    assert DRAFT_ROUNDS == 4


def test_skipped_marker_is_string():
    assert isinstance(SKIPPED_MARKER, str) and SKIPPED_MARKER


# ── generate_snake_order ─────────────────────────────────────────────


def test_generate_snake_order_three_players_four_rounds():
    order = generate_snake_order([10, 20, 30])
    assert order == [
        [1, 10], [1, 20], [1, 30],
        [2, 30], [2, 20], [2, 10],
        [3, 10], [3, 20], [3, 30],
        [4, 30], [4, 20], [4, 10],
    ]


def test_generate_snake_order_custom_rounds():
    order = generate_snake_order([1, 2], rounds=2)
    assert order == [[1, 1], [1, 2], [2, 2], [2, 1]]


def test_generate_snake_order_single_round():
    order = generate_snake_order([1, 2, 3], rounds=1)
    assert order == [[1, 1], [1, 2], [1, 3]]


def test_generate_snake_order_empty_players():
    assert generate_snake_order([]) == []


def test_generate_snake_order_round_count_matches():
    """Every player picks exactly ``rounds`` times."""
    players = [11, 22, 33, 44]
    order = generate_snake_order(players, rounds=4)
    assert len(order) == len(players) * 4
    for p in players:
        assert sum(1 for _, pid in order if pid == p) == 4


# ── is_duplicate ─────────────────────────────────────────────────────


def test_is_duplicate_case_insensitive_match():
    assert is_duplicate("Pizza", ["sushi", "PIZZA", "tacos"]) is True


def test_is_duplicate_whitespace_trimmed():
    assert is_duplicate("  pizza  ", ["pizza"]) is True


def test_is_duplicate_not_found():
    assert is_duplicate("burger", ["pizza", "sushi"]) is False


def test_is_duplicate_against_empty_list():
    assert is_duplicate("pizza", []) is False


# ── find_who_picked ──────────────────────────────────────────────────


def test_find_who_picked_returns_uid_str_of_owner():
    boards = {
        "1": ["Pizza", None, None, None],
        "2": ["Sushi", "Tacos", None, None],
    }
    assert find_who_picked("pizza", boards) == "1"
    assert find_who_picked("TACOS", boards) == "2"


def test_find_who_picked_skips_skipped_markers():
    """A slot holding the SKIPPED_MARKER must not match any input."""
    boards = {"1": [SKIPPED_MARKER, None, None, None]}
    assert find_who_picked(SKIPPED_MARKER, boards) is None


def test_find_who_picked_returns_none_when_not_found():
    boards = {"1": ["Pizza", None, None, None]}
    assert find_who_picked("Burger", boards) is None


def test_find_who_picked_skips_none_slots():
    boards = {"1": [None, None, None, None]}
    assert find_who_picked("anything", boards) is None


def test_find_who_picked_empty_boards_dict():
    assert find_who_picked("anything", {}) is None


# ── eligible_voters ──────────────────────────────────────────────────


def test_eligible_voters_includes_players_with_any_real_pick():
    players = [1, 2, 3]
    boards = {
        "1": ["Pizza", None, None, None],
        "2": [SKIPPED_MARKER, SKIPPED_MARKER, SKIPPED_MARKER, SKIPPED_MARKER],
        "3": [None, None, None, None],
    }
    assert eligible_voters(players, boards) == [1]


def test_eligible_voters_preserves_input_order():
    players = [3, 1, 2]
    boards = {
        "1": ["A", None, None, None],
        "2": ["B", None, None, None],
        "3": ["C", None, None, None],
    }
    assert eligible_voters(players, boards) == [3, 1, 2]


def test_eligible_voters_handles_missing_board():
    """A player whose uid isn't in ``boards`` is treated as having no picks."""
    assert eligible_voters([42], {}) == []


def test_eligible_voters_empty_player_list():
    assert eligible_voters([], {}) == []


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_single_winner():
    votes = {1: 10, 2: 10, 3: 20}
    winners, max_v, results = tally_votes(votes, eligible=[10, 20, 30])
    assert winners == [10]
    assert max_v == 2
    assert results == [(10, 2), (20, 1), (30, 0)]


def test_tally_votes_tie_returns_all_winners():
    votes = {1: 10, 2: 20}
    winners, max_v, _ = tally_votes(votes, eligible=[10, 20])
    assert set(winners) == {10, 20}
    assert max_v == 1


def test_tally_votes_no_votes_returns_empty_winners():
    winners, max_v, results = tally_votes({}, eligible=[10, 20])
    assert winners == []
    assert max_v == 0
    # Zero-vote players still show up in the results list, sorted by votes
    assert sorted(r[0] for r in results) == [10, 20]
    assert all(v == 0 for _, v in results)


def test_tally_votes_results_sorted_highest_first():
    votes = {1: 30, 2: 30, 3: 20, 4: 10}
    _, _, results = tally_votes(votes, eligible=[10, 20, 30])
    vals = [v for _, v in results]
    assert vals == sorted(vals, reverse=True)
    # 30 has 2 votes; 20 and 10 each have 1
    by_uid = dict(results)
    assert by_uid[30] == 2
    assert by_uid[20] == 1
    assert by_uid[10] == 1


# ── compute_recap_stats ──────────────────────────────────────────────


def test_compute_recap_stats_first_pick_present_when_round1_filled():
    draft_order = [[1, 10], [1, 20], [2, 20], [2, 10]]
    boards = {"10": ["Pizza", None], "20": [None, None]}
    stats = compute_recap_stats(
        draft_order, boards, all_picks=["Pizza"],
        pick_times={}, skipped=[], votes={},
        name_resolver=_name_resolver,
    )
    assert stats["first_pick"] == {"pick": "Pizza", "player": "User10"}


def test_compute_recap_stats_first_pick_absent_when_round1_skipped():
    draft_order = [[1, 10], [1, 20]]
    boards = {"10": [SKIPPED_MARKER, None], "20": [None, None]}
    stats = compute_recap_stats(
        draft_order, boards, all_picks=["dummy"],
        pick_times={}, skipped=["10_1"], votes={},
        name_resolver=_name_resolver,
    )
    assert "first_pick" not in stats


def test_compute_recap_stats_first_pick_absent_when_no_picks():
    """When no picks ever happened, the first_pick key is omitted."""
    stats = compute_recap_stats(
        [[1, 10]], {"10": [None]}, all_picks=[], pick_times={},
        skipped=[], votes={}, name_resolver=_name_resolver,
    )
    assert "first_pick" not in stats


def test_compute_recap_stats_skipped_count_and_names():
    stats = compute_recap_stats(
        [], {}, all_picks=[], pick_times={},
        skipped=["10_1", "20_2", "10_3"], votes={},
        name_resolver=_name_resolver,
    )
    assert stats["skipped_count"] == 3
    # Two unique players, dedup preserves first-seen order
    assert stats["skipped_names"] == ["User10", "User20"]


def test_compute_recap_stats_skipped_count_zero_when_none():
    stats = compute_recap_stats(
        [], {}, [], {}, skipped=[], votes={},
        name_resolver=_name_resolver,
    )
    assert stats["skipped_count"] == 0
    assert "skipped_names" not in stats


def test_compute_recap_stats_fastest_and_slowest():
    boards = {"10": ["A", "B"], "20": ["C", "D"]}
    pick_times = {"10_1": 5.0, "10_2": 12.0, "20_1": 2.5, "20_2": None}
    stats = compute_recap_stats(
        [[1, 10], [1, 20], [2, 20], [2, 10]],
        boards, all_picks=["A", "B", "C"], pick_times=pick_times,
        skipped=[], votes={}, name_resolver=_name_resolver,
    )
    # Fastest: 20_1 (2.5s) → "C"
    assert stats["fastest"]["pick"] == "C"
    assert stats["fastest"]["player"] == "User20"
    assert stats["fastest"]["time"] == pytest.approx(2.5)
    # Slowest: 10_2 (12s) → "B"
    assert stats["slowest"]["pick"] == "B"
    assert stats["slowest"]["player"] == "User10"
    assert stats["slowest"]["time"] == pytest.approx(12.0)


def test_compute_recap_stats_fast_slow_absent_when_no_timed_picks():
    """All None pick_times (e.g. every pick was skipped) → no fast/slow."""
    stats = compute_recap_stats(
        [], {}, [], pick_times={"10_1": None}, skipped=["10_1"], votes={},
    )
    assert "fastest" not in stats
    assert "slowest" not in stats


def test_compute_recap_stats_fastest_handles_multi_digit_uid():
    """``rsplit("_", 1)`` must work for uids that include digits anywhere."""
    boards = {"123456789": ["Pick!"]}
    pick_times: dict[str, float | None] = {"123456789_1": 4.2}
    stats = compute_recap_stats(
        [[1, 123456789]], boards, ["Pick!"], pick_times,
        skipped=[], votes={}, name_resolver=_name_resolver,
    )
    assert stats["fastest"]["pick"] == "Pick!"
    assert stats["fastest"]["player"] == "User123456789"


def test_compute_recap_stats_unanimous_flag_when_one_target():
    stats = compute_recap_stats(
        [], {}, [], {}, [], votes={1: 99, 2: 99, 3: 99},
    )
    assert stats.get("unanimous") is True
    assert "vote_split" not in stats


def test_compute_recap_stats_vote_split_flag_when_multi_target():
    stats = compute_recap_stats(
        [], {}, [], {}, [], votes={1: 10, 2: 20, 3: 30},
    )
    assert stats.get("vote_split") == 3
    assert "unanimous" not in stats


def test_compute_recap_stats_no_unanimous_or_split_when_no_votes():
    stats = compute_recap_stats([], {}, [], {}, [], votes={})
    assert "unanimous" not in stats
    assert "vote_split" not in stats


def test_compute_recap_stats_default_name_resolver_uses_str_uid():
    """When name_resolver is None, ``str(uid)`` is used in stat fields."""
    stats = compute_recap_stats(
        [[1, 42]],
        {"42": ["Pizza"]},
        all_picks=["Pizza"],
        pick_times={"42_1": 3.0},
        skipped=["42_1"],  # also test skipped_names path
        votes={},
    )
    assert stats["first_pick"]["player"] == "42"
    assert stats["fastest"]["player"] == "42"
    assert stats["skipped_names"] == ["42"]


def test_compute_recap_stats_pick_info_handles_out_of_range_round():
    """If a pick_times key points past the end of the board, the
    pick text falls back to ``"?"`` instead of raising."""
    boards = {"10": ["only_one"]}  # board is length 1
    pick_times: dict[str, float | None] = {"10_5": 3.3}
    stats = compute_recap_stats(
        [], boards, ["only_one"], pick_times, [], {},
        name_resolver=_name_resolver,
    )
    assert stats["fastest"]["pick"] == "?"


# ── clamp_settings ───────────────────────────────────────────────────


def test_clamp_settings_within_range_unchanged():
    assert clamp_settings(60, 30) == (60, 30)


def test_clamp_settings_below_min_pulled_up():
    assert clamp_settings(1, 1) == (10, 10)


def test_clamp_settings_above_max_pushed_down():
    assert clamp_settings(9999, 9999) == (120, 60)


def test_clamp_settings_boundary_values():
    assert clamp_settings(10, 10) == (10, 10)
    assert clamp_settings(120, 60) == (120, 60)


# ── render_draft_board ───────────────────────────────────────────────


def test_render_draft_board_marks_active_player():
    players = [(1, "Alice"), (2, "Bob")]
    boards = {"1": ["A", None, None, None], "2": [None, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=1)
    assert "**Draft Board:**" in out
    # The active player's line has the target emoji marker
    lines = out.split("\n")
    alice_line = next(line for line in lines if "Alice" in line)
    bob_line = next(line for line in lines if "Bob" in line)
    assert "\U0001f3af" in alice_line
    assert "\U0001f3af" not in bob_line


def test_render_draft_board_truncates_long_names():
    players = [(1, "X" * 30)]
    boards = {"1": [None, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=None)
    # Name caps at 16 chars (with the last replaced by ".")
    assert "X" * 30 not in out
    assert "." in out


def test_render_draft_board_truncates_long_picks():
    players = [(1, "Alice")]
    boards = {"1": ["A" * 30, None, None, None]}
    out = render_draft_board(players, boards, 1)
    assert "..." in out
    # The full 30-char string is not rendered
    assert "A" * 30 not in out


def test_render_draft_board_shows_skipped_label():
    players = [(1, "Alice")]
    boards = {"1": [SKIPPED_MARKER, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=None)
    assert "*Skipped*" in out


def test_render_draft_board_shows_dash_for_empty_slots():
    players = [(1, "Alice")]
    boards = {"1": [None, None, None, None]}
    out = render_draft_board(players, boards, None)
    assert "—" in out


def test_render_draft_board_empty_players():
    out = render_draft_board([], {}, None)
    assert "**Draft Board:**" in out


def test_render_draft_board_uses_default_empty_board_for_missing_uid():
    """A player whose uid isn't in boards still renders 4 dash slots."""
    players = [(99, "Ghost")]
    out = render_draft_board(players, {}, active_player_id=None)
    # 4 empty slots = four "—" segments
    assert out.count("—") == 4


# ── build_join_embed ─────────────────────────────────────────────────


def test_build_join_embed_no_topic():
    embed = build_join_embed("Alice", [], topic=None)
    assert embed.title is not None
    assert "MT. RUSHMORE DRAFT" in embed.title
    assert embed.description is not None
    assert "snake draft" in embed.description
    # Players field present with count 0
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert any("Players (0)" in n for n in by_name)


def test_build_join_embed_with_topic_renders_in_description():
    embed = build_join_embed("Alice", ["Bob"], topic="Snacks")
    assert embed.description is not None
    assert "Snacks" in embed.description


def test_build_join_embed_players_field_lists_names():
    embed = build_join_embed("Alice", ["Bob", "Carol"], topic=None)
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    players_field = next(v for n, v in by_name.items() if "Players (2)" in n)
    assert "Bob" in players_field
    assert "Carol" in players_field


def test_build_join_embed_footer_includes_host():
    embed = build_join_embed("Alice", [], topic=None)
    assert embed.footer.text is not None
    assert "Alice" in embed.footer.text


def test_build_join_embed_escapes_markdown_in_topic_and_host():
    embed = build_join_embed("*Host*", [], topic="*Topic*")
    assert embed.description is not None
    # Escaped markdown adds backslashes around the asterisks
    assert "\\*" in embed.description


# ── build_draft_embed ────────────────────────────────────────────────


def test_build_draft_embed_renders_round_and_topic():
    players = [(1, "Alice"), (2, "Bob")]
    boards = {"1": ["Pizza", None, None, None], "2": [None] * 4}
    embed = build_draft_embed(
        host_name="Host",
        topic="Snacks",
        players=players,
        boards=boards,
        active_player_id=2,
        active_player_name="Bob",
        round_num=1,
        timer_secs=30,
    )
    assert embed.title is not None
    assert "Snacks" in embed.title
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    # Timer field carries the round / total
    timer_val = next(v for n, v in by_name.items() if "Timer" in n)
    assert f"Round 1/{DRAFT_ROUNDS}" in timer_val
    # Now Picking field exists for the active player
    assert any("Now Picking" in n for n in by_name)


def test_build_draft_embed_omits_now_picking_when_no_active_player():
    embed = build_draft_embed(
        host_name="Host", topic="t", players=[(1, "Alice")],
        boards={"1": [None] * 4},
        active_player_id=None, active_player_name=None,
        round_num=1, timer_secs=30,
    )
    field_names = [f.name for f in embed.fields]
    assert not any("Now Picking" in (n or "") for n in field_names)


# ── build_final_boards_embed ─────────────────────────────────────────


def test_build_final_boards_embed_one_field_per_player():
    players = [(1, "Alice"), (2, "Bob")]
    boards = {
        "1": ["A1", "A2", "A3", "A4"],
        "2": ["B1", SKIPPED_MARKER, None, "B4"],
    }
    embed = build_final_boards_embed("Host", "Topic", players, boards)
    assert len(embed.fields) == 2
    # Alice's full board renders all four picks
    alice_field = next(f for f in embed.fields if "Alice" in (f.name or ""))
    assert "A1" in (alice_field.value or "")
    assert "A4" in (alice_field.value or "")
    # Bob's board shows skipped + dash + real picks
    bob_field = next(f for f in embed.fields if "Bob" in (f.name or ""))
    assert "*Skipped*" in (bob_field.value or "")
    assert "—" in (bob_field.value or "")


def test_build_final_boards_embed_handles_missing_board():
    """A player without an entry in ``boards`` renders all dashes."""
    players = [(1, "Ghost")]
    embed = build_final_boards_embed("Host", "Topic", players, {})
    assert len(embed.fields) == 1
    assert (embed.fields[0].value or "").count("—") == DRAFT_ROUNDS


# ── build_vote_embed ─────────────────────────────────────────────────


def test_build_vote_embed_has_vote_field():
    embed = build_vote_embed("Host", "Snacks", timer_secs=30)
    assert embed.title is not None
    assert "VOTE" in embed.title
    field_names = [f.name for f in embed.fields]
    assert "Timer" in field_names
    assert "Vote" in field_names


# ── build_winner_embed ───────────────────────────────────────────────


def test_build_winner_embed_single_winner():
    embed = build_winner_embed(
        host_name="Host", topic="Snacks",
        winner_names=["Alice"], winner_votes=3,
        winner_boards=[["Pizza", "Sushi", "Tacos", "Burgers"]],
        all_results=[("Alice", 3), ("Bob", 1)],
    )
    field_names = [f.name or "" for f in embed.fields]
    assert any("Alice wins" in n for n in field_names)
    assert any("3 votes" in n for n in field_names)
    # Full Results field exists
    assert any("Full Results" in n for n in field_names)


def test_build_winner_embed_single_vote_is_singular():
    embed = build_winner_embed(
        "Host", "t", ["Alice"], 1,
        [["X", "Y", "Z", "W"]],
        [("Alice", 1)],
    )
    field_names = [f.name or "" for f in embed.fields]
    assert any("1 vote" in n and "votes" not in n for n in field_names)


def test_build_winner_embed_tied_winners_render_both_boards():
    embed = build_winner_embed(
        "Host", "t", ["Alice", "Bob"], 2,
        [["A", None, None, None], ["B", None, None, None]],
        [("Alice", 2), ("Bob", 2)],
    )
    field_names = [f.name or "" for f in embed.fields]
    assert any("Alice & Bob" in n for n in field_names)
    winner_value = next(f.value or "" for f in embed.fields if "wins" in (f.name or ""))
    # Both winner boards render in the same field
    assert "A" in winner_value and "B" in winner_value


def test_build_winner_embed_empty_results_renders_dash():
    embed = build_winner_embed(
        "Host", "t", [], 0, [], all_results=[],
    )
    # Field values fall back to "—" when there are no winners or results
    values = [f.value or "" for f in embed.fields]
    assert "—" in values


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_summary_includes_topic_and_winner():
    embed = build_recap_embed(
        host_name="Host", topic="Snacks", player_count=4,
        duration_secs=185.0,
        winner_names=["Alice"], winner_votes=3,
        winner_boards=[["Pizza", "Sushi", "Tacos", "Burgers"]],
        stats={"skipped_count": 0},
    )
    summary_field = next(f for f in embed.fields if (f.name or "") == "Summary")
    val = summary_field.value or ""
    assert "Snacks" in val
    assert "Alice" in val
    assert "3m 5s" in val  # 185 seconds rounded
    assert "Players: **4**" in val


def test_build_recap_embed_singular_vote_label():
    embed = build_recap_embed(
        "Host", "t", 2, 30.0, ["Alice"], 1,
        [["A", "B", "C", "D"]], stats={"skipped_count": 0},
    )
    val = (embed.fields[0].value or "")
    assert "1 vote" in val and "1 votes" not in val


def test_build_recap_embed_renders_all_stat_fields():
    stats = {
        "first_pick": {"pick": "Pizza", "player": "Alice"},
        "skipped_count": 2,
        "skipped_names": ["Bob", "Carol"],
        "fastest": {"pick": "Sushi", "player": "Dave", "time": 1.2},
        "slowest": {"pick": "Tacos", "player": "Erin", "time": 28.9},
        "vote_split": 2,
    }
    embed = build_recap_embed(
        "Host", "Topic", 5, 60.0, ["Alice"], 2,
        [["A", "B", "C", "D"]], stats=stats,
    )
    stats_field = next(
        (f for f in embed.fields if (f.name or "").endswith("Draft Stats")),
        None,
    )
    assert stats_field is not None
    val = stats_field.value or ""
    assert "First Overall Pick" in val
    assert "Pizza" in val and "Alice" in val
    assert "Skipped Picks" in val and "Bob" in val and "Carol" in val
    assert "Fastest Pick" in val and "Sushi" in val and "1.2s" in val
    assert "Slowest Pick" in val and "Tacos" in val and "28.9s" in val
    assert "2-way split" in val


def test_build_recap_embed_unanimous_overrides_vote_split():
    """When stats has unanimous=True, the unanimous line shows (not split)."""
    embed = build_recap_embed(
        "Host", "Topic", 3, 30.0, ["Alice"], 3,
        [["A", "B", "C", "D"]],
        stats={"skipped_count": 0, "unanimous": True},
    )
    stats_field = next(
        f for f in embed.fields if (f.name or "").endswith("Draft Stats")
    )
    val = stats_field.value or ""
    assert "Unanimous Vote: Yes" in val
    assert "way split" not in val


def test_build_recap_embed_no_stat_lines_when_only_zero_skipped():
    """skipped_count=0 still renders the Skipped Picks line; verify the
    Draft Stats field is present in that case (since skipped_count is
    not None)."""
    embed = build_recap_embed(
        "Host", "Topic", 3, 30.0, ["Alice"], 2,
        [["A", "B", "C", "D"]], stats={"skipped_count": 0},
    )
    stats_field = next(
        (f for f in embed.fields if (f.name or "").endswith("Draft Stats")),
        None,
    )
    assert stats_field is not None
    assert "Skipped Picks: **0**" in (stats_field.value or "")


def test_build_recap_embed_empty_stats_omits_draft_stats_field():
    """A fully empty stats dict means no Draft Stats field at all."""
    embed = build_recap_embed(
        "Host", "Topic", 3, 30.0, ["Alice"], 2,
        [["A", "B", "C", "D"]], stats={},
    )
    field_names = [f.name or "" for f in embed.fields]
    assert not any(n.endswith("Draft Stats") for n in field_names)
