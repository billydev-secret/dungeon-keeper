"""Tests for the extracted Name Your Price pure-logic modules.

Covers ``bot_modules/games_price/logic.py`` (price parsing/formatting,
ladder + stats, vote winner tally, recap awards + highlight) and
``bot_modules/games_price/embeds.py`` (start/scenario/reveal/vote/round-
results/recap embed builders). Mirrors the pressure_cooker /
games_hottakes / games_ttl pattern: the cog file stays thin; this
module proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games_price.embeds import (
    build_recap_embed,
    build_reveal_embed,
    build_round_results_embed,
    build_scenario_embed,
    build_start_embed,
    build_vote_embed,
)
from bot_modules.games_price.logic import (
    MAX_PRICE,
    MIN_PRICE,
    build_ladder,
    collect_all_players,
    compute_highlight,
    compute_recap_awards,
    format_price,
    ladder_stats,
    parse_price,
    price_label,
    tally_winners,
)


# ── parse_price ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", 0),
        ("100", 100),
        ("42", 42),
        ("$500", 500),
        ("$1,000", 1000),
        ("1,234,567", 1_234_567),
        ("  $50  ", 50),
        ("5k", 5_000),
        ("5K", 5_000),
        ("1.5k", 1_500),
        ("2m", 2_000_000),
        ("2.5M", 2_500_000),
        ("1B", MAX_PRICE),  # 1B == 1_000_000_000 clamps to MAX_PRICE (999_999_999)
        ("3 million", 3_000_000),
        ("4 billion", MAX_PRICE),  # clamped
        ("1 BILLION", MAX_PRICE),  # clamped
        ("$1.2m", 1_200_000),
    ],
)
def test_parse_price_recognises_common_forms(raw: str, expected: int) -> None:
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "$abc", "k", "m", "$$", "$,"])
def test_parse_price_returns_none_on_garbage(raw: str) -> None:
    assert parse_price(raw) is None


@pytest.mark.parametrize("raw", ["abck", "xyzm", "$??b"])
def test_parse_price_returns_none_when_suffix_leaves_bad_number(raw: str) -> None:
    """The suffix matches but the residue can't be parsed as a float."""
    assert parse_price(raw) is None


def test_parse_price_handles_none() -> None:
    assert parse_price(None) is None  # type: ignore[arg-type]


def test_parse_price_clamps_below_min() -> None:
    assert parse_price("-100") == MIN_PRICE
    assert parse_price("-5k") == MIN_PRICE


def test_parse_price_clamps_above_max() -> None:
    # 10B is way over the cap
    assert parse_price("10b") == MAX_PRICE
    assert parse_price("1000000000000") == MAX_PRICE


def test_parse_price_exact_cap() -> None:
    assert parse_price(str(MAX_PRICE)) == MAX_PRICE


# ── format_price ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "amount,expected",
    [
        (0, "$0"),
        (1, "$1"),
        (42, "$42"),
        (999, "$999"),
        (1_000, "$1,000"),
        (12_345, "$12,345"),
        (999_999, "$999,999"),
        (1_000_000, "$1.0M"),
        (2_500_000, "$2.5M"),
        (1_000_000_000, "$1.0B"),
        (1_500_000_000, "$1.5B"),
    ],
)
def test_format_price(amount: int, expected: str) -> None:
    assert format_price(amount) == expected


# ── price_label ──────────────────────────────────────────────────────


def test_price_label_zero_gets_free_flavour() -> None:
    assert price_label(0) == "$0 (free?!)"


def test_price_label_huge_gets_refuse_flavour() -> None:
    assert price_label(999_000_000).endswith("(absolutely not)")
    assert price_label(MAX_PRICE).endswith("(absolutely not)")


def test_price_label_plain_in_middle() -> None:
    # 500 is neither free nor over the refuse threshold
    assert price_label(500) == "$500"
    assert price_label(50_000) == "$50,000"


# ── build_ladder ─────────────────────────────────────────────────────


def test_build_ladder_sorts_by_amount() -> None:
    prices = {10: 500, 20: 100, 30: 250}
    assert build_ladder(prices) == [(20, 100), (30, 250), (10, 500)]


def test_build_ladder_breaks_amount_ties_by_uid() -> None:
    prices = {7: 100, 3: 100, 5: 100}
    assert build_ladder(prices) == [(3, 100), (5, 100), (7, 100)]


def test_build_ladder_empty_returns_empty() -> None:
    assert build_ladder({}) == []


# ── ladder_stats ─────────────────────────────────────────────────────


def test_ladder_stats_empty_returns_none() -> None:
    assert ladder_stats([]) is None


def test_ladder_stats_single_amount() -> None:
    s = ladder_stats([42])
    assert s == {"low": 42, "high": 42, "median": 42, "mean": 42}


def test_ladder_stats_multiple_amounts() -> None:
    s = ladder_stats([100, 200, 300, 400])
    assert s is not None
    assert s["low"] == 100
    assert s["high"] == 400
    assert s["median"] == 250
    assert s["mean"] == 250


def test_ladder_stats_truncates_floats_to_int() -> None:
    # median of [1, 2] is 1.5 → int 1
    s = ladder_stats([1, 2])
    assert s is not None
    assert s["median"] == 1
    # mean of [1, 2, 4] is 7/3 ≈ 2.33 → int 2
    s2 = ladder_stats([1, 2, 4])
    assert s2 is not None
    assert s2["mean"] == 2


# ── tally_winners ────────────────────────────────────────────────────


def test_tally_winners_no_votes() -> None:
    winners, max_votes = tally_winners({})
    assert winners == []
    assert max_votes == 0


def test_tally_winners_single_winner() -> None:
    votes = {1: 100, 2: 100, 3: 200}
    winners, max_votes = tally_winners(votes)
    assert winners == [100]
    assert max_votes == 2


def test_tally_winners_two_way_tie() -> None:
    votes = {1: 100, 2: 200, 3: 100, 4: 200}
    winners, max_votes = tally_winners(votes)
    assert sorted(winners) == [100, 200]
    assert max_votes == 2


def test_tally_winners_three_way_tie() -> None:
    votes = {1: 100, 2: 200, 3: 300}
    winners, max_votes = tally_winners(votes)
    assert sorted(winners) == [100, 200, 300]
    assert max_votes == 1


def test_tally_winners_unanimous() -> None:
    votes = {1: 50, 2: 50, 3: 50}
    winners, max_votes = tally_winners(votes)
    assert winners == [50]
    assert max_votes == 3


# ── compute_recap_awards ─────────────────────────────────────────────


def test_compute_recap_awards_empty_inputs() -> None:
    awards = compute_recap_awards({}, {})
    assert awards == {}


def test_compute_recap_awards_reasonable_unhinged_single_winner() -> None:
    scores = {
        "reasonable_wins": {"1": 3, "2": 1},
        "unhinged_wins": {"2": 2},
    }
    awards = compute_recap_awards({}, scores)
    assert "reasonable" in awards
    label, uids, detail = awards["reasonable"]
    assert label.startswith("🎯")
    assert uids == [1]
    assert detail == "won 3 rounds"

    label2, uids2, detail2 = awards["unhinged"]
    assert uids2 == [2]
    assert detail2 == "won 2 rounds"


def test_compute_recap_awards_single_round_uses_singular_round_word() -> None:
    scores = {"reasonable_wins": {"5": 1}, "unhinged_wins": {}}
    awards = compute_recap_awards({}, scores)
    _label, _uids, detail = awards["reasonable"]
    assert detail == "won 1 round"


def test_compute_recap_awards_ties_include_all_winners() -> None:
    scores = {
        "reasonable_wins": {"10": 2, "20": 2, "30": 1},
        "unhinged_wins": {},
    }
    awards = compute_recap_awards({}, scores)
    _label, uids, _detail = awards["reasonable"]
    assert sorted(uids) == [10, 20]


def test_compute_recap_awards_spender_and_cheapest_one_round() -> None:
    rounds_data = {
        "1": {"prices": {"100": 50_000, "200": 100}},
    }
    awards = compute_recap_awards(rounds_data, {})
    _label, spender_uids, spender_detail = awards["spender"]
    assert spender_uids == [100]
    assert "$50,000" in spender_detail

    _label, cheap_uids, cheap_detail = awards["cheapest"]
    assert cheap_uids == [200]
    assert "$100" in cheap_detail

    # Single round means no consistent/wildest
    assert "consistent" not in awards
    assert "wildest" not in awards


def test_compute_recap_awards_consistent_and_wildest_need_two_rounds() -> None:
    rounds_data = {
        "1": {"prices": {"100": 100, "200": 50}},
        "2": {"prices": {"100": 110, "200": 5_000}},
    }
    awards = compute_recap_awards(rounds_data, {})
    assert "consistent" in awards
    assert "wildest" in awards
    _label, cons_uids, _detail = awards["consistent"]
    _label, wild_uids, _detail = awards["wildest"]
    # Player 100 swung only 10; player 200 swung a lot
    assert cons_uids == [100]
    assert wild_uids == [200]


def test_compute_recap_awards_skips_stddev_if_only_one_round_per_player() -> None:
    # Different players each only appear in one round
    rounds_data = {
        "1": {"prices": {"100": 50}},
        "2": {"prices": {"200": 75}},
    }
    awards = compute_recap_awards(rounds_data, {})
    assert "spender" in awards
    assert "cheapest" in awards
    assert "consistent" not in awards
    assert "wildest" not in awards


def test_compute_recap_awards_ignores_malformed_uid_keys() -> None:
    rounds_data = {
        "1": {"prices": {"notanumber": 50, "100": 200}},
    }
    awards = compute_recap_awards(rounds_data, {})
    # Only the valid uid should show up
    _label, spender_uids, _ = awards["spender"]
    assert spender_uids == [100]


# ── compute_highlight ────────────────────────────────────────────────


def test_compute_highlight_empty_returns_none() -> None:
    assert compute_highlight({}) is None


def test_compute_highlight_single_submission_round_returns_none() -> None:
    rounds_data = {"1": {"prices": {"100": 50}}}
    assert compute_highlight(rounds_data) is None


def test_compute_highlight_picks_widest_spread() -> None:
    rounds_data = {
        "1": {"prices": {"100": 10, "200": 20}},  # spread 10
        "2": {"prices": {"100": 5, "200": 1000}},  # spread 995
        "3": {"prices": {"100": 50, "200": 60}},  # spread 10
    }
    hi = compute_highlight(rounds_data)
    assert hi is not None
    rnum, lo, high = hi
    assert rnum == "2"
    assert lo == 5
    assert high == 1000


def test_compute_highlight_first_wins_on_tie() -> None:
    # Both rounds have spread 100 — the first encountered wins.
    rounds_data = {
        "1": {"prices": {"100": 0, "200": 100}},
        "2": {"prices": {"100": 200, "200": 300}},
    }
    hi = compute_highlight(rounds_data)
    assert hi is not None
    rnum, _lo, _hi = hi
    assert rnum == "1"


# ── collect_all_players ──────────────────────────────────────────────


def test_collect_all_players_empty() -> None:
    assert collect_all_players({}) == set()


def test_collect_all_players_dedupes_across_rounds() -> None:
    rounds_data = {
        "1": {"prices": {"100": 50, "200": 75}},
        "2": {"prices": {"100": 99, "300": 1}},
    }
    assert collect_all_players(rounds_data) == {100, 200, 300}


def test_collect_all_players_skips_malformed() -> None:
    rounds_data = {
        "1": {"prices": {"100": 50, "junk": 99}},
    }
    assert collect_all_players(rounds_data) == {100}


# ── build_start_embed ────────────────────────────────────────────────


def test_build_start_embed_shows_host_and_round() -> None:
    embed = build_start_embed("Alice", round_num=1, total_rounds=5)
    assert embed.title is not None
    assert "NAME YOUR PRICE" in embed.title
    assert embed.description is not None
    assert "Alice" in embed.description
    assert "Round 1/5" in embed.description


def test_build_start_embed_has_status_field() -> None:
    embed = build_start_embed("Alice", 1, 3)
    by_name = {f.name: f.value for f in embed.fields}
    assert "Status" in by_name
    assert by_name["Status"] is not None
    assert "Starting" in by_name["Status"]


def test_build_start_embed_footer_mentions_host() -> None:
    embed = build_start_embed("Bob", 1, 1)
    assert embed.footer.text is not None
    assert "Bob" in embed.footer.text


# ── build_scenario_embed ─────────────────────────────────────────────


def test_build_scenario_embed_renders_scenario_and_submission_count() -> None:
    embed = build_scenario_embed(
        host_name="Alice",
        scenario="How much to eat a bug?",
        round_num=2,
        total_rounds=5,
        timer_secs=30,
        submitted=3,
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Scenario"] is not None
    assert "eat a bug" in by_name["Scenario"]
    assert by_name["Submissions"] == "💵 Submitted: **3**"


def test_build_scenario_embed_shows_total_when_provided() -> None:
    embed = build_scenario_embed(
        "Alice", "x", 1, 1, 30, submitted=2, total_players=4
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Submissions"] == "💵 Submitted: **2**/4"


def test_build_scenario_embed_escapes_markdown_in_scenario() -> None:
    embed = build_scenario_embed("Alice", "**bold**", 1, 1, 30, submitted=0)
    by_name = {f.name: f.value for f in embed.fields}
    scen = by_name["Scenario"]
    assert scen is not None
    assert "\\*\\*bold\\*\\*" in scen


def test_build_scenario_embed_title_includes_round_count() -> None:
    embed = build_scenario_embed("Alice", "x", 2, 7, 30, submitted=0)
    assert embed.title is not None
    assert "Round 2/7" in embed.title


# ── build_reveal_embed ───────────────────────────────────────────────


def test_build_reveal_embed_renders_ladder_lines() -> None:
    ladder = [("Alice", 50), ("Bob", 1000), ("Carol", 2_000_000)]
    embed = build_reveal_embed("Host", "scen", 1, 3, ladder)
    by_name = {f.name: f.value for f in embed.fields}
    ladder_text = by_name["💵 Price Ladder"]
    assert ladder_text is not None
    assert "Alice" in ladder_text
    assert "Bob" in ladder_text
    assert "Carol" in ladder_text


def test_build_reveal_embed_zero_amount_shows_free_flavour() -> None:
    embed = build_reveal_embed("Host", "scen", 1, 1, [("Alice", 0)])
    by_name = {f.name: f.value for f in embed.fields}
    text = by_name["💵 Price Ladder"]
    assert text is not None
    assert "free?!" in text


def test_build_reveal_embed_huge_amount_shows_refuse_flavour() -> None:
    embed = build_reveal_embed("Host", "scen", 1, 1, [("Alice", 999_000_000)])
    by_name = {f.name: f.value for f in embed.fields}
    text = by_name["💵 Price Ladder"]
    assert text is not None
    assert "absolutely not" in text


def test_build_reveal_embed_omits_stats_for_empty_ladder() -> None:
    embed = build_reveal_embed("Host", "scen", 1, 1, [])
    field_names = {f.name for f in embed.fields}
    assert "📊 Stats" not in field_names


def test_build_reveal_embed_includes_stats_for_populated_ladder() -> None:
    ladder = [("Alice", 100), ("Bob", 500), ("Carol", 900)]
    embed = build_reveal_embed("Host", "scen", 1, 1, ladder)
    by_name = {f.name: f.value for f in embed.fields}
    assert "📊 Stats" in by_name
    stats = by_name["📊 Stats"]
    assert stats is not None
    assert "Spread:" in stats
    assert "Median:" in stats
    assert "Average:" in stats


def test_build_reveal_embed_escapes_markdown_in_names() -> None:
    embed = build_reveal_embed("Host", "scen", 1, 1, [("**Alice**", 100)])
    by_name = {f.name: f.value for f in embed.fields}
    text = by_name["💵 Price Ladder"]
    assert text is not None
    assert "\\*\\*Alice\\*\\*" in text


# ── build_vote_embed ─────────────────────────────────────────────────


def test_build_vote_embed_renders_scenario_and_prompt() -> None:
    embed = build_vote_embed("Host", "the scenario", 2, 5, 20)
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Scenario"] is not None
    assert "the scenario" in by_name["Scenario"]
    assert by_name["Vote"] is not None
    assert "Most Reasonable" in by_name["Vote"]
    assert "Most Unhinged" in by_name["Vote"]


def test_build_vote_embed_title_shows_round() -> None:
    embed = build_vote_embed("Host", "x", 3, 7, 20)
    assert embed.title is not None
    assert "Round 3/7" in embed.title


# ── build_round_results_embed ────────────────────────────────────────


def test_build_round_results_embed_pluralises_vote_word() -> None:
    embed = build_round_results_embed(
        "Host", 1, 3,
        "Alice", 100, 1,   # 1 vote → "vote" singular
        "Bob", 5000, 4,    # 4 votes → "votes" plural
    )
    by_name = {f.name: f.value for f in embed.fields}
    r = by_name["🎯 Most Reasonable"]
    u = by_name["🤯 Most Unhinged"]
    assert r is not None
    assert u is not None
    assert "1 vote" in r and "1 votes" not in r
    assert "4 votes" in u


def test_build_round_results_embed_includes_winner_prices() -> None:
    embed = build_round_results_embed(
        "Host", 1, 1,
        "Alice", 100, 1,
        "Bob", 5_000_000, 1,
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["🎯 Most Reasonable"] is not None
    assert by_name["🤯 Most Unhinged"] is not None
    assert "$100" in by_name["🎯 Most Reasonable"]
    assert "$5.0M" in by_name["🤯 Most Unhinged"]


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_summary_and_no_awards() -> None:
    embed = build_recap_embed("Host", rounds_played=3, player_count=4, awards={}, highlight=None)
    by_name = {f.name: f.value for f in embed.fields}
    assert "Summary" in by_name
    assert by_name["Summary"] is not None
    assert "3" in by_name["Summary"]
    assert "4" in by_name["Summary"]
    # No awards field when empty
    assert "🏆 Awards" not in by_name


def test_build_recap_embed_renders_award_lines() -> None:
    awards = {
        "reasonable": ("🎯 Most Reasonable (overall):", "Alice", "won 2 rounds"),
        "spender": ("💸 Biggest Spender:", "Bob", "avg $5,000"),
    }
    embed = build_recap_embed("Host", 3, 4, awards, highlight=None)
    by_name = {f.name: f.value for f in embed.fields}
    aw = by_name["🏆 Awards"]
    assert aw is not None
    assert "Alice" in aw
    assert "Bob" in aw
    assert "won 2 rounds" in aw


def test_build_recap_embed_skips_awards_with_empty_name() -> None:
    # An empty name shouldn't render a line — matches the cog's old
    # "if name:" guard.
    awards = {
        "reasonable": ("🎯 Most Reasonable:", "", "n/a"),
        "spender": ("💸 Spender:", "Alice", "avg $100"),
    }
    embed = build_recap_embed("Host", 1, 1, awards, highlight=None)
    by_name = {f.name: f.value for f in embed.fields}
    aw = by_name["🏆 Awards"]
    assert aw is not None
    # Reasonable line was suppressed (empty name)
    assert "Most Reasonable" not in aw
    assert "Alice" in aw


def test_build_recap_embed_omits_awards_field_when_all_empty() -> None:
    awards = {
        "reasonable": ("🎯 Most Reasonable:", "", "n/a"),
    }
    embed = build_recap_embed("Host", 1, 1, awards, highlight=None)
    field_names = {f.name for f in embed.fields}
    assert "🏆 Awards" not in field_names


def test_build_recap_embed_renders_highlight_when_present() -> None:
    embed = build_recap_embed(
        "Host", 2, 3, awards={}, highlight="Round 1 had the widest spread"
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert "💡 Highlight" in by_name
    hl = by_name["💡 Highlight"]
    assert hl is not None
    assert "widest spread" in hl


def test_build_recap_embed_escapes_markdown_in_award_name() -> None:
    awards = {
        "reasonable": ("🎯 Most Reasonable:", "**Bold**", "n/a"),
    }
    embed = build_recap_embed("Host", 1, 1, awards, highlight=None)
    by_name = {f.name: f.value for f in embed.fields}
    aw = by_name["🏆 Awards"]
    assert aw is not None
    assert "\\*\\*Bold\\*\\*" in aw


def test_build_recap_embed_footer_mentions_host() -> None:
    embed = build_recap_embed("Host", 1, 1, {}, None)
    assert embed.footer.text is not None
    assert "Host" in embed.footer.text


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_price_cog as price_cog  # noqa: E402
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


async def test_show_recap_pays_all_submitters(monkeypatch, sync_db_path):
    """Recap pays everyone who submitted a price in any round."""
    spy = AsyncMock()
    monkeypatch.setattr(price_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {
        "rounds": {"1": {"prices": {"1": 100, "2": 200}}, "2": {"prices": {"3": 50}}},
        "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
    }
    gid = await create_game(bot.games_db, 100, 1, "price", payload=payload)
    cog = price_cog.PriceCog(bot)  # type: ignore[arg-type]
    channel = FakeChannel(id=100)
    await cog._show_recap(gid, 1, "Host", channel, None, {})
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert set(call.kwargs["player_ids"]) == {1, 2, 3}
    assert call.kwargs["bot"] is bot
