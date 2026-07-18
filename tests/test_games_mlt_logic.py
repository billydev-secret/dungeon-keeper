"""Tests for the extracted Most Likely To pure-logic modules.

Covers ``bot_modules/games_mlt/logic.py`` (lobby membership, vote
recording, per-round tally, crown winners, payload codecs, prompt
queue) and ``bot_modules/games_mlt/embeds.py`` (join, round, closed,
results embeds).

Mirrors the games_nhie template since MLT is the closest sibling
(per-round single-target voting). The cog stays thin; this module
proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games_mlt.embeds import (
    build_closed_embed,
    build_join_embed,
    build_results_embed,
    build_round_embed,
)
from bot_modules.games_mlt.logic import (
    MIN_PLAYERS,
    add_player,
    apply_vote,
    bump_crowns,
    can_start,
    encode_round_votes,
    find_round_winners,
    is_eligible_voter,
    pop_next_prompt,
    queue_prompt,
    remove_player,
    tally_votes,
)


# ── add_player / remove_player ───────────────────────────────────────


def test_add_player_appends_new_uid():
    players: list[int] = []
    assert add_player(players, 42) is True
    assert players == [42]


def test_add_player_idempotent_re_press():
    players: list[int] = [42]
    assert add_player(players, 42) is False
    assert players == [42]


def test_add_player_preserves_order():
    players: list[int] = [1, 2]
    add_player(players, 3)
    add_player(players, 4)
    assert players == [1, 2, 3, 4]


def test_remove_player_removes_existing():
    players: list[int] = [1, 2, 3]
    assert remove_player(players, 2) is True
    assert players == [1, 3]


def test_remove_player_noop_when_absent():
    players: list[int] = [1, 3]
    assert remove_player(players, 2) is False
    assert players == [1, 3]


def test_add_then_remove_round_trip():
    players: list[int] = []
    add_player(players, 7)
    add_player(players, 9)
    remove_player(players, 7)
    assert players == [9]


# ── can_start ────────────────────────────────────────────────────────


def test_can_start_false_below_minimum():
    assert can_start([1, 2]) is False


def test_can_start_true_at_minimum():
    assert can_start([1, 2, 3]) is True


def test_can_start_true_above_minimum():
    assert can_start([1, 2, 3, 4, 5]) is True


def test_can_start_min_players_constant_matches_cog_threshold():
    """The cog's hard-coded check used 3 — guard against drift."""
    assert MIN_PLAYERS == 3


def test_can_start_custom_threshold():
    assert can_start([1, 2], min_players=2) is True
    assert can_start([1], min_players=2) is False


# ── apply_vote ───────────────────────────────────────────────────────


def test_apply_vote_records_new_vote_returns_false():
    """A first-time voter is not 'changed' — only switching is."""
    votes: dict[int, int] = {}
    changed = apply_vote(votes, voter_id=1, target_id=2)
    assert changed is False
    assert votes == {1: 2}


def test_apply_vote_switching_target_returns_true():
    votes: dict[int, int] = {1: 2}
    changed = apply_vote(votes, voter_id=1, target_id=3)
    assert changed is True
    assert votes == {1: 3}


def test_apply_vote_re_voting_same_target_returns_false():
    """An idempotent re-press for the same target doesn't count as a switch."""
    votes: dict[int, int] = {1: 2}
    changed = apply_vote(votes, voter_id=1, target_id=2)
    assert changed is False
    assert votes == {1: 2}


def test_apply_vote_two_voters_independent():
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=2, target_id=10)
    assert votes == {1: 10, 2: 10}


def test_apply_vote_allows_self_vote():
    """The HOW_TO_PLAY explicitly allows voting for yourself."""
    votes: dict[int, int] = {}
    changed = apply_vote(votes, voter_id=42, target_id=42)
    assert changed is False
    assert votes == {42: 42}


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_counts_per_target():
    votes = {1: 10, 2: 10, 3: 20}
    tally = tally_votes(votes, players=[10, 20, 30])
    assert tally == {10: 2, 20: 1, 30: 0}


def test_tally_votes_zero_for_uncovered_players():
    """Every player in the pool gets an entry, even with no votes."""
    tally = tally_votes({}, players=[1, 2, 3])
    assert tally == {1: 0, 2: 0, 3: 0}


def test_tally_votes_empty_players_empty_votes_empty_tally():
    assert tally_votes({}, players=[]) == {}


def test_tally_votes_includes_off_pool_target():
    """If a vote sneaks past eligibility, the target still appears."""
    votes = {1: 999}
    tally = tally_votes(votes, players=[1])
    assert tally == {1: 0, 999: 1}


def test_tally_votes_handles_unanimous_vote():
    votes = {1: 5, 2: 5, 3: 5, 4: 5}
    tally = tally_votes(votes, players=[5, 6])
    assert tally == {5: 4, 6: 0}


# ── find_round_winners ───────────────────────────────────────────────


def test_find_round_winners_single_winner():
    tally = {1: 3, 2: 1, 3: 0}
    assert find_round_winners(tally) == [1]


def test_find_round_winners_tie_returns_all_top():
    tally = {1: 2, 2: 2, 3: 1}
    winners = find_round_winners(tally)
    assert set(winners) == {1, 2}
    assert len(winners) == 2


def test_find_round_winners_empty_when_no_votes_cast():
    """Top score is 0 → no crowns awarded."""
    tally = {1: 0, 2: 0, 3: 0}
    assert find_round_winners(tally) == []


def test_find_round_winners_empty_when_tally_empty():
    assert find_round_winners({}) == []


def test_find_round_winners_three_way_tie():
    tally = {1: 1, 2: 1, 3: 1}
    winners = find_round_winners(tally)
    assert sorted(winners) == [1, 2, 3]


# ── bump_crowns ──────────────────────────────────────────────────────


def test_bump_crowns_increments_per_user():
    crowns: dict[str, int] = {}
    bump_crowns(crowns, [1, 2])
    assert crowns == {"1": 1, "2": 1}


def test_bump_crowns_preserves_existing_counts():
    crowns: dict[str, int] = {"1": 3}
    bump_crowns(crowns, [1])
    assert crowns == {"1": 4}


def test_bump_crowns_handles_tie_winners():
    crowns: dict[str, int] = {"1": 1}
    bump_crowns(crowns, [1, 2, 3])
    assert crowns == {"1": 2, "2": 1, "3": 1}


def test_bump_crowns_empty_winners_noop():
    crowns: dict[str, int] = {"1": 5}
    bump_crowns(crowns, [])
    assert crowns == {"1": 5}


def test_bump_crowns_string_keys_match_payload_convention():
    """The persisted payload uses str keys — guard against int leakage."""
    crowns: dict[str, int] = {}
    bump_crowns(crowns, [123456789])
    assert "123456789" in crowns
    assert 123456789 not in crowns


# ── encode_round_votes ───────────────────────────────────────────────


def test_encode_round_votes_stringifies_voter_keys():
    encoded = encode_round_votes({1: 10, 2: 20})
    assert encoded == {"1": 10, "2": 20}


def test_encode_round_votes_keeps_target_as_int():
    encoded = encode_round_votes({1: 999})
    assert encoded["1"] == 999
    assert isinstance(encoded["1"], int)


def test_encode_round_votes_empty_dict_round_trip():
    assert encode_round_votes({}) == {}


# ── is_eligible_voter ────────────────────────────────────────────────


def test_is_eligible_voter_true_when_in_pool():
    assert is_eligible_voter(42, [1, 42, 99]) is True


def test_is_eligible_voter_false_when_outside_pool():
    assert is_eligible_voter(42, [1, 2, 3]) is False


def test_is_eligible_voter_false_for_empty_pool():
    assert is_eligible_voter(42, []) is False


# ── queue_prompt / pop_next_prompt ───────────────────────────────────


def test_queue_prompt_appends_and_returns_count():
    queued: list[str] = []
    n = queue_prompt(queued, "win a staring contest")
    assert n == 1
    assert queued == ["win a staring contest"]


def test_queue_prompt_strips_whitespace():
    queued: list[str] = []
    queue_prompt(queued, "   say something nice   ")
    assert queued == ["say something nice"]


def test_queue_prompt_ignores_empty_after_strip():
    queued: list[str] = ["x"]
    n = queue_prompt(queued, "    ")
    assert n == 1  # unchanged
    assert queued == ["x"]


def test_queue_prompt_returns_running_total():
    queued: list[str] = []
    assert queue_prompt(queued, "a") == 1
    assert queue_prompt(queued, "b") == 2
    assert queue_prompt(queued, "c") == 3


def test_pop_next_prompt_returns_first_and_rest():
    next_p, remaining = pop_next_prompt(["a", "b", "c"])
    assert next_p == "a"
    assert remaining == ["b", "c"]


def test_pop_next_prompt_empty_queue():
    next_p, remaining = pop_next_prompt([])
    assert next_p is None
    assert remaining == []


def test_pop_next_prompt_does_not_mutate_original():
    """The original list must be untouched — the cog mutates it elsewhere."""
    original = ["a", "b"]
    pop_next_prompt(original)
    assert original == ["a", "b"]


def test_pop_next_prompt_single_item_leaves_empty_remaining():
    next_p, remaining = pop_next_prompt(["only"])
    assert next_p == "only"
    assert remaining == []


# ── build_join_embed ─────────────────────────────────────────────────


def test_build_join_embed_title_contains_game_name():
    embed = build_join_embed("Alice", [])
    assert embed.title is not None
    assert "MOST LIKELY TO" in embed.title


def test_build_join_embed_renders_host_and_players():
    embed = build_join_embed("Alice", ["Bob", "Charlie"])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert "Players (2)" in by_name
    assert "Bob, Charlie" == by_name["Players (2)"]


def test_build_join_embed_dash_when_no_players():
    embed = build_join_embed("Alice", [])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Players (0)"] == "—"


def test_build_join_embed_has_footer():
    embed = build_join_embed("Alice", [])
    assert embed.footer.text is not None
    assert "Most Likely To" in embed.footer.text


# ── build_round_embed ────────────────────────────────────────────────


def test_build_round_embed_active_title_no_suffix():
    embed = build_round_embed("staring contest", round_num=1, vote_count=0)
    assert embed.title is not None
    assert "MOST LIKELY TO" in embed.title
    assert "ROUND OVER" not in embed.title


def test_build_round_embed_closed_title_has_suffix():
    embed = build_round_embed(
        "staring contest", round_num=1, vote_count=0, closed=True
    )
    assert embed.title is not None
    assert "ROUND OVER" in embed.title


def test_build_round_embed_renders_prompt_and_round_fields():
    embed = build_round_embed("staring contest", round_num=7, vote_count=3)
    by_name = {f.name: (f.value or "") for f in embed.fields}
    assert by_name["Prompt"] == "staring contest"
    assert "7" in by_name["Round"]
    assert "3 votes" in by_name["Round"]


def test_build_round_embed_escapes_markdown_in_prompt():
    embed = build_round_embed("*sneaky* prompt", round_num=1, vote_count=0)
    by_name = {f.name: (f.value or "") for f in embed.fields}
    assert "\\*sneaky\\*" in by_name["Prompt"]


def test_build_round_embed_has_footer_with_round_number():
    embed = build_round_embed("x", round_num=4, vote_count=0)
    assert embed.footer.text is not None
    assert "Round 4" in embed.footer.text


def test_build_round_embed_active_vs_closed_color_differs():
    active = build_round_embed("x", round_num=1, vote_count=0, closed=False)
    closed = build_round_embed("x", round_num=1, vote_count=0, closed=True)
    assert active.color != closed.color


# ── build_closed_embed ───────────────────────────────────────────────


def test_build_closed_embed_title_says_closed():
    embed = build_closed_embed("x", round_num=1, vote_count=0)
    assert embed.title is not None
    assert "CLOSED" in embed.title


def test_build_closed_embed_color_differs_from_round_over():
    closed = build_closed_embed("x", round_num=1, vote_count=0)
    round_over = build_round_embed("x", round_num=1, vote_count=0, closed=True)
    assert closed.color != round_over.color


def test_build_closed_embed_preserves_prompt_and_round():
    embed = build_closed_embed("staring contest", round_num=2, vote_count=5)
    by_name = {f.name: (f.value or "") for f in embed.fields}
    assert by_name["Prompt"] == "staring contest"
    assert "5 votes" in by_name["Round"]


# ── build_results_embed ──────────────────────────────────────────────


def test_build_results_embed_lists_descending_vote_counts():
    embed = build_results_embed(
        prompt="staring contest",
        round_num=1,
        tally={100: 1, 200: 3, 300: 2},
    )
    assert embed.description is not None
    lines = embed.description.split("\n")
    # User 200 (3 votes) appears first, then 300 (2), then 100 (1)
    assert "200" in lines[0]
    assert "300" in lines[1]
    assert "100" in lines[2]


def test_build_results_embed_crowns_only_top():
    embed = build_results_embed(
        prompt="x", round_num=1, tally={1: 3, 2: 1}
    )
    assert embed.description is not None
    lines = embed.description.split("\n")
    assert "👑" in lines[0]
    assert "👑" not in lines[1]


def test_build_results_embed_crowns_all_tied_top():
    embed = build_results_embed(
        prompt="x", round_num=1, tally={1: 2, 2: 2, 3: 1}
    )
    assert embed.description is not None
    lines = embed.description.split("\n")
    # First two are tied at the top — both crowned
    assert "👑" in lines[0]
    assert "👑" in lines[1]
    # Third has fewer — no crown
    assert "👑" not in lines[2]


def test_build_results_embed_no_crown_when_zero_votes():
    """No one voted — nobody gets a crown, even though counts tie at 0."""
    embed = build_results_embed(
        prompt="x", round_num=1, tally={1: 0, 2: 0}
    )
    assert embed.description is not None
    assert "👑" not in embed.description


def test_build_results_embed_empty_tally_shows_placeholder():
    embed = build_results_embed(prompt="x", round_num=1, tally={})
    assert embed.description == "No votes cast."


def test_build_results_embed_title_contains_prompt():
    embed = build_results_embed(
        prompt="staring contest", round_num=1, tally={1: 1}
    )
    assert embed.title is not None
    assert "staring contest" in embed.title


def test_build_results_embed_footer_has_round_number():
    embed = build_results_embed(prompt="x", round_num=9, tally={1: 1})
    assert embed.footer.text is not None
    assert "Round 9" in embed.footer.text


def test_build_results_embed_renders_vote_counts():
    embed = build_results_embed(prompt="x", round_num=1, tally={1: 5, 2: 2})
    assert embed.description is not None
    assert "5 votes" in embed.description
    assert "2 votes" in embed.description


# ── sanity / integration ─────────────────────────────────────────────


def test_full_round_flow_single_winner_then_crown():
    """Cast votes, tally, find winners, bump crowns — end to end."""
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=2, target_id=10)
    apply_vote(votes, voter_id=3, target_id=20)
    tally = tally_votes(votes, players=[10, 20, 30])
    assert tally == {10: 2, 20: 1, 30: 0}
    winners = find_round_winners(tally)
    assert winners == [10]
    crowns: dict[str, int] = {}
    bump_crowns(crowns, winners)
    assert crowns == {"10": 1}


def test_full_round_flow_tie_awards_two_crowns():
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=2, target_id=20)
    tally = tally_votes(votes, players=[10, 20])
    winners = find_round_winners(tally)
    crowns: dict[str, int] = {}
    bump_crowns(crowns, winners)
    assert crowns == {"10": 1, "20": 1}


def test_voter_changes_pick_only_last_vote_counted():
    """The cog calls apply_vote on every press — the tally must reflect
    only the final pick, not any history."""
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=1, target_id=20)  # switched
    apply_vote(votes, voter_id=1, target_id=30)  # switched again
    tally = tally_votes(votes, players=[10, 20, 30])
    assert tally == {10: 0, 20: 0, 30: 1}


def test_queue_then_pop_full_cycle():
    queued: list[str] = []
    queue_prompt(queued, "first")
    queue_prompt(queued, "second")
    queue_prompt(queued, "third")
    next_p, remaining = pop_next_prompt(queued)
    assert next_p == "first"
    assert remaining == ["second", "third"]


@pytest.mark.parametrize("vote_kind_unused", [None])
def test_lobby_minimum_enforced_then_passes(vote_kind_unused):
    """Two players can't start; adding a third unlocks it."""
    players: list[int] = []
    add_player(players, 1)
    add_player(players, 2)
    assert can_start(players) is False
    add_player(players, 3)
    assert can_start(players) is True


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_mlt_cog as mlt_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_run_round_empty_bank_pays_all_voters(monkeypatch, sync_db_path):
    """When the prompt bank runs dry, the game ends paying everyone who voted
    across all completed rounds (not just current survivors)."""
    spy = AsyncMock()
    monkeypatch.setattr(mlt_cog, "end_game", spy)
    monkeypatch.setattr(mlt_cog, "get_mlt_prompt", AsyncMock(return_value=None))
    bot = _SpyBot(sync_db_path)
    payload = {
        "rounds": {
            "1": {"votes": {"1": "2", "2": "1"}, "prompt": "x"},
            "2": {"votes": {"3": "1"}, "prompt": "y"},
        },
        "crowns": {}, "players": [1, 3],  # player 2 left mid-game
    }
    gid = await create_game(bot.games_db, 100, 1, "mlt", payload=payload)
    bot.active_views[gid] = object()
    cog = mlt_cog.MLTCog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())
    await cog._run_round(None, gid, 1, "Host", 3, [1, 3], channel)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3]  # includes departed voter 2
    assert call.kwargs["bot"] is bot
