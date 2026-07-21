"""Tests for the extracted Hot Takes pure-logic modules.

Covers ``bot_modules/games_hottakes/logic.py`` (add_take, shuffle_takes,
tally_votes, compute_recap_summary) and
``bot_modules/games_hottakes/embeds.py`` (lobby, vote, recap embed
builders). Mirrors the pressure_cooker / games_traditional pattern: the
cog file stays thin; this module proves the extracted pieces work
without spinning up Discord.
"""

from __future__ import annotations

import math
import random

import discord
import pytest

from bot_modules.games.constants import (
    PHASE_JOINING,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)
from bot_modules.games_hottakes.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_vote_embed,
)
from bot_modules.games_hottakes.logic import (
    VOTE_KEYS,
    VOTE_LABELS,
    VOTE_VALUES,
    add_take,
    compute_recap_summary,
    shuffle_takes,
    tally_votes,
)


# ── constants sanity ─────────────────────────────────────────────────


def test_vote_constants_are_same_length():
    assert len(VOTE_LABELS) == len(VOTE_VALUES) == len(VOTE_KEYS) == 5


def test_vote_values_are_strictly_ascending():
    assert VOTE_VALUES == sorted(VOTE_VALUES)
    assert len(set(VOTE_VALUES)) == len(VOTE_VALUES)


# ── add_take ─────────────────────────────────────────────────────────


def test_add_take_initializes_takes_list_and_returns_count():
    payload: dict = {}
    count = add_take(payload, user_id=42, text="Pineapple belongs on pizza")
    assert count == 1
    assert payload["takes"] == [
        {"user_id": 42, "text": "Pineapple belongs on pizza", "display_order": 0}
    ]


def test_add_take_appends_with_correct_display_order():
    payload: dict = {}
    add_take(payload, 1, "first")
    add_take(payload, 2, "second")
    count = add_take(payload, 3, "third")
    assert count == 3
    assert [t["display_order"] for t in payload["takes"]] == [0, 1, 2]
    assert [t["text"] for t in payload["takes"]] == ["first", "second", "third"]


def test_add_take_preserves_existing_takes():
    payload = {"takes": [{"user_id": 1, "text": "old", "display_order": 0}]}
    add_take(payload, 2, "new")
    assert len(payload["takes"]) == 2
    assert payload["takes"][1]["text"] == "new"
    assert payload["takes"][1]["display_order"] == 1


# ── shuffle_takes ────────────────────────────────────────────────────


def test_shuffle_takes_returns_same_elements():
    takes = [
        {"user_id": 1, "text": "a", "display_order": 0},
        {"user_id": 2, "text": "b", "display_order": 1},
        {"user_id": 3, "text": "c", "display_order": 2},
    ]
    shuffled = shuffle_takes(takes, rng=random.Random(0))
    assert {t["text"] for t in shuffled} == {"a", "b", "c"}
    assert len(shuffled) == 3


def test_shuffle_takes_rewrites_display_order_to_position():
    takes = [
        {"user_id": 1, "text": "a", "display_order": 99},
        {"user_id": 2, "text": "b", "display_order": 99},
        {"user_id": 3, "text": "c", "display_order": 99},
    ]
    shuffled = shuffle_takes(takes, rng=random.Random(0))
    assert [t["display_order"] for t in shuffled] == [0, 1, 2]


def test_shuffle_takes_with_seeded_rng_is_deterministic():
    takes = [
        {"user_id": i, "text": str(i), "display_order": 0} for i in range(5)
    ]
    a = shuffle_takes([dict(t) for t in takes], rng=random.Random(42))
    b = shuffle_takes([dict(t) for t in takes], rng=random.Random(42))
    assert [t["text"] for t in a] == [t["text"] for t in b]


def test_shuffle_takes_default_rng_does_not_crash():
    """No rng argument falls back to the module-level random."""
    takes = [{"user_id": 1, "text": "a", "display_order": 0}]
    out = shuffle_takes(takes)
    assert len(out) == 1
    assert out[0]["display_order"] == 0


def test_shuffle_takes_empty_list_returns_empty():
    assert shuffle_takes([]) == []


def test_shuffle_takes_does_not_mutate_original_length():
    """We pass a copy in but inner dicts are shared — the caller can
    rely on the input list reference still pointing at the same items."""
    original = [
        {"user_id": 1, "text": "a", "display_order": 5},
        {"user_id": 2, "text": "b", "display_order": 6},
    ]
    shuffled = shuffle_takes(original, rng=random.Random(0))
    # Original list reference still has 2 items
    assert len(original) == 2
    # But its dicts may have been mutated in-place (they're shared with
    # the shuffled list) — both refs point to display_order in [0, 1]
    assert sorted(t["display_order"] for t in original) == [0, 1]
    assert sorted(t["display_order"] for t in shuffled) == [0, 1]


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_no_votes_returns_zeroed_counts_and_zero_avg_std():
    counts, avg, std = tally_votes({})
    assert counts == [0, 0, 0, 0, 0]
    assert avg == 0.0
    assert std == 0.0


def test_tally_votes_single_voter_returns_zero_std():
    counts, avg, std = tally_votes({100: 4})  # one vote on '🔥' (value 5)
    assert counts == [0, 0, 0, 0, 1]
    assert avg == 5.0
    # stdev of a single sample is 0 by our convention
    assert std == 0.0


def test_tally_votes_multiple_voters_computes_weighted_avg():
    """Two voters at 1, two at 5 → avg = (1+1+5+5)/4 = 3.0."""
    votes = {1: 0, 2: 0, 3: 4, 4: 4}
    counts, avg, std = tally_votes(votes)
    assert counts == [2, 0, 0, 0, 2]
    assert avg == 3.0
    # Sample stdev of [1, 1, 5, 5] ~ 2.309
    assert math.isclose(std, 2.309401076758503, rel_tol=1e-6)


def test_tally_votes_unanimous_vote_yields_zero_std():
    votes = {1: 2, 2: 2, 3: 2}  # all '😐' (value 3)
    counts, avg, std = tally_votes(votes)
    assert counts == [0, 0, 3, 0, 0]
    assert avg == 3.0
    assert std == 0.0


def test_tally_votes_ignores_out_of_range_indexes():
    """Defensive: if a stale view ever recorded an out-of-range index
    we silently skip it rather than crashing."""
    votes = {1: 4, 2: 99, 3: -1}
    counts, avg, _std = tally_votes(votes)
    assert counts == [0, 0, 0, 0, 1]
    assert avg == 5.0


def test_tally_votes_respects_custom_vote_values():
    """Custom scale: 0/100 binary."""
    votes = {1: 0, 2: 1}
    counts, avg, std = tally_votes(votes, vote_values=[0, 100])
    assert counts == [1, 1]
    assert avg == 50.0
    assert math.isclose(std, 70.71067811865476, rel_tol=1e-6)


# ── compute_recap_summary ────────────────────────────────────────────


def test_compute_recap_summary_returns_none_for_empty_results():
    assert compute_recap_summary([]) is None


def test_compute_recap_summary_single_result_has_no_most_divisive():
    results = [
        {"text": "lone take", "avg": 3.0, "std": 0.0, "voters": [1, 2]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["hottest"]["text"] == "lone take"
    assert summary["coldest"]["text"] == "lone take"
    assert summary["most_divisive"] is None
    assert summary["total_voters"] == {1, 2}
    assert summary["total_takes"] == 1


def test_compute_recap_summary_picks_hottest_and_coldest():
    results = [
        {"text": "mild", "avg": 3.0, "std": 0.5, "voters": [1]},
        {"text": "cold", "avg": 1.5, "std": 0.2, "voters": [2]},
        {"text": "hot", "avg": 4.5, "std": 0.3, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["hottest"]["text"] == "hot"
    assert summary["coldest"]["text"] == "cold"
    assert summary["total_voters"] == {1, 2, 3}


def test_compute_recap_summary_most_divisive_uses_highest_std():
    results = [
        {"text": "unanimous", "avg": 3.0, "std": 0.0, "voters": [1]},
        {"text": "split", "avg": 3.0, "std": 2.5, "voters": [2]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_divisive"]["text"] == "split"


def test_compute_recap_summary_divisive_tiebreak_prefers_distance_from_midpoint():
    """Two takes with equal std: the picker prefers the one whose avg
    is farthest from the scale midpoint."""
    # Midpoint for VOTE_VALUES [1..5] is 3.0
    results = [
        {"text": "near-mid", "avg": 3.1, "std": 1.0, "voters": [1]},
        {"text": "far-from-mid", "avg": 4.8, "std": 1.0, "voters": [2]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_divisive"]["text"] == "far-from-mid"


def test_compute_recap_summary_dedupes_voters_across_results():
    results = [
        {"text": "a", "avg": 2.0, "std": 0.0, "voters": [1, 2]},
        {"text": "b", "avg": 4.0, "std": 0.0, "voters": [2, 3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["total_voters"] == {1, 2, 3}
    assert summary["total_takes"] == 2


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_shows_host_name_and_zero_submissions():
    embed = build_lobby_embed("Alice")
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert by_name["Submissions"] == "0"
    assert embed.title is not None
    assert "Hot Takes" in embed.title


def test_build_lobby_embed_uses_provided_submission_count():
    embed = build_lobby_embed("Alice", submission_count=7)
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Submissions"] == "7"


def test_build_lobby_embed_has_anonymous_footer():
    embed = build_lobby_embed("Alice")
    assert embed.footer.text is not None
    assert "Anonymous" in embed.footer.text


# ── build_vote_embed ─────────────────────────────────────────────────


def test_build_vote_embed_open_has_playing_title_no_suffix():
    embed = build_vote_embed("My take", take_num=1, total_takes=3, votes_by_user={})
    assert embed.title is not None
    assert "Hot Take #1" in embed.title
    assert "Round Over" not in embed.title


def test_build_vote_embed_closed_appends_round_over():
    embed = build_vote_embed(
        "My take", take_num=2, total_takes=4, votes_by_user={1: 4}, closed=True
    )
    assert embed.title is not None
    assert "Round Over" in embed.title


def test_build_vote_embed_escapes_markdown_in_take_text():
    embed = build_vote_embed(
        "**bold** _italic_", take_num=1, total_takes=1, votes_by_user={}
    )
    take_field = next(f for f in embed.fields if f.name == "Take")
    assert take_field.value is not None
    # asterisks and underscores should be escaped
    assert "\\*\\*bold\\*\\*" in take_field.value
    assert "\\_italic\\_" in take_field.value


def test_build_vote_embed_progress_shows_take_number_over_total():
    embed = build_vote_embed("t", take_num=3, total_takes=5, votes_by_user={})
    progress_field = next(f for f in embed.fields if f.name == "Progress")
    assert progress_field.value == "Take 3/5"


def test_build_vote_embed_votes_field_renders_all_labels():
    embed = build_vote_embed("t", take_num=1, total_takes=1, votes_by_user={1: 4})
    votes_field = next(f for f in embed.fields if f.name == "Votes")
    assert votes_field.value is not None
    for label in VOTE_LABELS:
        assert label in votes_field.value


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_returns_none_for_empty_results():
    assert build_recap_embed([]) is None


def test_build_recap_embed_single_result_omits_most_divisive_field():
    results = [
        {"text": "only one", "avg": 3.0, "std": 0.0, "voters": [1, 2]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert "🔥 Hottest Take" in by_name
    assert "🧊 Coldest Take" in by_name
    assert "⚡ Most Divisive" not in by_name
    assert by_name["Total Takes"] == "1"
    assert by_name["Total Voters"] == "2"


def test_build_recap_embed_multiple_results_includes_most_divisive():
    results = [
        {"text": "mild", "avg": 3.0, "std": 0.1, "voters": [1]},
        {"text": "hot", "avg": 4.5, "std": 0.2, "voters": [2]},
        {"text": "divisive", "avg": 3.5, "std": 2.5, "voters": [3]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert "⚡ Most Divisive" in by_name
    most_div = by_name["⚡ Most Divisive"]
    assert most_div is not None
    assert "divisive" in most_div


def test_build_recap_embed_shows_avg_to_one_decimal():
    results = [{"text": "x", "avg": 3.456, "std": 0.0, "voters": [1]}]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    # Hottest formatting: "x" (avg 3.5/5)
    hottest = by_name["🔥 Hottest Take"]
    assert hottest is not None
    assert "3.5/5" in hottest


def test_build_recap_embed_dedupes_total_voters():
    results = [
        {"text": "a", "avg": 2.0, "std": 0.0, "voters": [1, 2, 3]},
        {"text": "b", "avg": 4.0, "std": 0.0, "voters": [2, 3, 4]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Total Voters"] == "4"
    assert by_name["Total Takes"] == "2"


# ── accent color migration (2026-07-21 ruling) ──────────────────────
#
# Hot Takes is a voting game: lobby / playing / results / recap are NOT
# win/loss, so every phase honors the passed guild accent. With no color
# passed, each builder falls back to its old PHASE_* constant.


ACCENT = discord.Color(0x123456)


def test_build_lobby_embed_honors_passed_accent():
    embed = build_lobby_embed("Alice", color=ACCENT)
    assert embed.color == ACCENT


def test_build_lobby_embed_falls_back_to_phase_joining():
    embed = build_lobby_embed("Alice")
    assert embed.color == discord.Color(PHASE_JOINING)


def test_build_vote_embed_open_honors_passed_accent():
    embed = build_vote_embed(
        "t", take_num=1, total_takes=1, votes_by_user={}, color=ACCENT
    )
    assert embed.color == ACCENT


def test_build_vote_embed_closed_honors_passed_accent():
    embed = build_vote_embed(
        "t", take_num=1, total_takes=1, votes_by_user={1: 4},
        closed=True, color=ACCENT,
    )
    assert embed.color == ACCENT


def test_build_vote_embed_falls_back_to_phase_playing_when_open():
    embed = build_vote_embed("t", take_num=1, total_takes=1, votes_by_user={})
    assert embed.color == discord.Color(PHASE_PLAYING)


def test_build_vote_embed_falls_back_to_phase_results_when_closed():
    embed = build_vote_embed(
        "t", take_num=1, total_takes=1, votes_by_user={1: 4}, closed=True
    )
    assert embed.color == discord.Color(PHASE_RESULTS)


def test_build_recap_embed_honors_passed_accent():
    results = [{"text": "x", "avg": 3.0, "std": 0.0, "voters": [1]}]
    embed = build_recap_embed(results, color=ACCENT)
    assert embed is not None
    assert embed.color == ACCENT


def test_build_recap_embed_falls_back_to_phase_recap():
    results = [{"text": "x", "avg": 3.0, "std": 0.0, "voters": [1]}]
    embed = build_recap_embed(results)
    assert embed is not None
    assert embed.color == discord.Color(PHASE_RECAP)


# ── parametrized: tally_votes covers the full scale ─────────────────


@pytest.mark.parametrize("idx,expected_avg", list(enumerate(VOTE_VALUES)))
def test_tally_votes_single_vote_avg_equals_scale_value(idx, expected_avg):
    counts, avg, _std = tally_votes({1: idx})
    assert counts[idx] == 1
    assert avg == expected_avg


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_hottakes_cog as hottakes_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_run_voting_pays_voters_and_authors(monkeypatch, sync_db_path):
    """Roster = voters plus take authors; the winning take's author may not have
    voted, so a voters-only set would drop their participation + win credit."""
    spy = AsyncMock()
    monkeypatch.setattr(hottakes_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {"takes": [{"text": "t1", "user_id": 9}], "results": []}
    gid = await create_game(bot.games_db, 100, 1, "hottakes", payload=payload)
    bot.active_views[gid] = object()  # placeholder so the loop's entry guard passes
    cog = hottakes_cog.HotTakesCog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(
        id=100, guild=None,
        send=AsyncMock(return_value=SimpleNamespace(id=999, edit=AsyncMock())),
    )
    task = asyncio.ensure_future(cog._run_voting(None, gid, 1, "Host", channel))
    try:
        view = None
        for _ in range(300):
            await asyncio.sleep(0.01)
            candidate = bot.active_views.get(gid)
            if isinstance(candidate, hottakes_cog.HotTakeVoteView):
                view = candidate
                break
        assert view is not None, "vote view was never created"
        view.votes = {1: 0, 2: 3}  # author 9 never voted
        await view.advance_callback(SimpleNamespace(edit=AsyncMock()))
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 9]
    assert call.kwargs["bot"] is bot
