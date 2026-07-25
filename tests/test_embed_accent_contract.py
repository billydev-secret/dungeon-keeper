"""Every pure embed builder honors a passed accent and falls back to its default.

One repo-wide contract (CLAUDE.md: new embeds take their color from
``resolve_accent_color``; red/green/etc. survive only where the color is
semantic). Until 2026-07 every game re-tested it with two copy-pasted tests
per builder — ~86 near-identical functions. A new builder now adds one
``case(...)`` row here instead.

Only builders whose color IS the accent belong in this table. Semantic-color
tests ("winner stays green regardless of accent") and resolver-wiring tests
(cogs patching ``resolve_accent_color``) stay in their feature files.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.cogs.games_legitlibs import rendering as ll_rendering
from bot_modules.economy import bounty_views as economy_bounty_views
from bot_modules.economy import pin_views as economy_pin_views
from bot_modules.games import constants as games_constants
from bot_modules.games_clapback import embeds as clapback_embeds
from bot_modules.games_fantasies import embeds as fantasies_embeds
from bot_modules.games_hottakes import embeds as hottakes_embeds
from bot_modules.games_mlt import embeds as mlt_embeds
from bot_modules.games_nhie import embeds as nhie_embeds
from bot_modules.games_price import embeds as price_embeds
from bot_modules.games_rushmore import embeds as rushmore_embeds
from bot_modules.games_traditional import embeds as traditional_embeds
from bot_modules.games_ttl import embeds as ttl_embeds
from bot_modules.games_wyr import embeds as wyr_embeds
from bot_modules.services import economy_service
from bot_modules.services import embeds as services_embeds
from bot_modules.services.risky_roll import formatters as risky_formatters
from bot_modules.services.risky_roll import models as risky_models

# Deliberately no builder defaults to this value.
ACCENT = discord.Color(0x5A32A8)


def case(case_id, build, fallback):
    """One builder under contract: id, ``build(**kw)`` thunk, fallback color.

    ``build`` is called once as ``build(color=ACCENT)`` (accent passthrough)
    and once as ``build()`` (fallback). ``fallback`` is the exact color the
    builder must default to, or None when the feature never had a fallback
    expectation (passthrough-only builders).
    """
    return pytest.param(build, fallback, id=case_id)


def _econ_settings():
    return economy_service.EconSettings(
        currency_emoji="💎", currency_name="gem", currency_plural="gems"
    )


def _risky_state(**kw):
    return risky_models.RiskyRollState(channel_id=100, guild_id=1, opener_id=10, **kw)


CASES = [
    # ── risky roll (accent param is named `accent`, adapted in the lambdas) ──
    case(
        "risky.open_round",
        lambda **kw: risky_formatters.build_embed(
            _risky_state(), None, accent=kw["color"]
        ) if kw else risky_formatters.build_embed(_risky_state()),
        discord.Color(0xDC3545),
    ),
    case(
        "risky.reroll",
        lambda **kw: risky_formatters.build_embed(
            _risky_state(reroll_user_ids={1, 2}), None, accent=kw["color"]
        ) if kw else risky_formatters.build_embed(_risky_state(reroll_user_ids={1, 2})),
        discord.Color(0xFF9800),
    ),
    case(
        "risky.round_over",
        lambda **kw: risky_formatters.build_embed(
            _risky_state(rolls={1: 90, 2: 5}, highest_user=1, lowest_user=2, is_open=False),
            None,
            accent=kw["color"],
        ) if kw else risky_formatters.build_embed(
            _risky_state(rolls={1: 90, 2: 5}, highest_user=1, lowest_user=2, is_open=False)
        ),
        discord.Color(0x546E7A),
    ),
    # ── legitlibs: quiplash ──────────────────────────────────────────────
    case(
        "quiplash.join",
        lambda **kw: ll_rendering.build_join_embed("Host", "T", 3, "quiplash", 1, 2, **kw),
        discord.Color(games_constants.PHASE_JOINING),
    ),
    case(
        "quiplash.fill",
        lambda **kw: ll_rendering.build_fill_embed("Host", "T", 3, 2, 0, **kw),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "quiplash.reveal",
        lambda **kw: ll_rendering.build_reveal_embed("T", 3, "body", 1, 1, **kw),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "quiplash.no_submissions",
        lambda **kw: ll_rendering.build_no_submissions_embed("T", 3, **kw),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    # ── legitlibs: classic ───────────────────────────────────────────────
    case(
        "ll.classic_fill",
        lambda **kw: ll_rendering.build_classic_fill_embed("Host", "T", 2, 3, 0, 111, **kw),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "ll.classic_rescue",
        lambda **kw: ll_rendering.build_classic_rescue_embed("T", 2, 2, [], 111, **kw),
        None,
    ),
    case(
        "ll.classic_rescue_fill",
        lambda **kw: ll_rendering.build_classic_rescue_fill_embed("T", 2, 0, 1, 111, **kw),
        None,
    ),
    case(
        "ll.classic_reveal",
        lambda **kw: ll_rendering.build_classic_reveal_embed("T", 2, "filled", ["A"], **kw),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    # ── clapback (passthrough-only: the file never tested fallbacks) ─────
    case(
        "clapback.lobby",
        lambda **kw: clapback_embeds.build_lobby_embed(
            "Alice", {"rounds": 5}, [], lambda uid: f"User{uid}", **kw
        ),
        None,
    ),
    case(
        "clapback.submit",
        lambda **kw: clapback_embeds.build_submit_embed(
            prompt="p", round_num=1, total_rounds=5, deadline_str="<t:1:R>",
            answers_in=0, total_players=3, **kw,
        ),
        None,
    ),
    case(
        "clapback.vote",
        lambda **kw: clapback_embeds.build_vote_embed(
            answer_a="a", answer_b="b", round_num=1, matchup_index=0,
            total_matchups=1, deadline_str="<t:1:R>", **kw,
        ),
        None,
    ),
    case(
        "clapback.scoreboard",
        lambda **kw: clapback_embeds.build_scoreboard_embed(
            {"scores": {"1": 10}}, 1, 5, bye_player=None, **kw
        ),
        None,
    ),
    case(
        "clapback.recap",
        lambda **kw: clapback_embeds.build_recap_embed(
            {"scores": {}, "clapbacks": {}, "round_history": [], "players": []},
            {"anonymous": False},
            lambda uid: f"User{uid}",
            **kw,
        ),
        None,
    ),
    case(
        "clapback.reveal_tie",
        lambda **kw: clapback_embeds.build_reveal_embed(
            result={
                "winner": None,
                "scores": {10: 50, 20: 50},
                "clapback": False,
                "vote_counts": {10: 1, 20: 1},
            },
            answers={"10": "a", "20": "b"},
            player_a=10,
            player_b=20,
            anonymous=False,
            name_resolver=lambda uid: f"User{uid}",
            **kw,
        ),
        None,
    ),
    # ── never have I ever ────────────────────────────────────────────────
    case(
        "nhie.round_active",
        lambda **kw: nhie_embeds.build_round_embed(
            statement="x", guilty=[], innocent=[], round_num=1, **kw
        ),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "nhie.round_closed",
        lambda **kw: nhie_embeds.build_round_embed(
            statement="x", guilty=[], innocent=[], round_num=1, closed=True, **kw
        ),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "nhie.closed",
        lambda **kw: nhie_embeds.build_closed_embed(
            statement="x", guilty=[], innocent=[], round_num=1, **kw
        ),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    case(
        "nhie.recap",
        lambda **kw: nhie_embeds.build_recap_embed(winner_id=42, guilt_scores={"42": 1}, **kw),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    # ── most likely to ───────────────────────────────────────────────────
    case(
        "mlt.join",
        lambda **kw: mlt_embeds.build_join_embed("Alice", [], **kw),
        discord.Color(games_constants.PHASE_JOINING),
    ),
    case(
        "mlt.round_active",
        lambda **kw: mlt_embeds.build_round_embed("x", round_num=1, vote_count=0, **kw),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "mlt.round_closed",
        lambda **kw: mlt_embeds.build_round_embed(
            "x", round_num=1, vote_count=0, closed=True, **kw
        ),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "mlt.closed",
        lambda **kw: mlt_embeds.build_closed_embed("x", round_num=1, vote_count=0, **kw),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    case(
        "mlt.results",
        lambda **kw: mlt_embeds.build_results_embed(prompt="x", round_num=1, tally={1: 1}, **kw),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "mlt.final_standings",
        lambda **kw: mlt_embeds.build_final_standings_embed({"1": 2}, **kw),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    case(
        "mlt.final_standings_empty",
        lambda **kw: mlt_embeds.build_final_standings_embed({}, **kw),
        None,
    ),
    # ── the price is right ───────────────────────────────────────────────
    case(
        "price.start",
        lambda **kw: price_embeds.build_start_embed("Host", 1, 5, **kw),
        None,
    ),
    case(
        "price.scenario",
        lambda **kw: price_embeds.build_scenario_embed(
            "Host", "scen", 1, 5, 30, submitted=0, **kw
        ),
        None,
    ),
    case(
        "price.reveal",
        lambda **kw: price_embeds.build_reveal_embed(
            "Host", "scen", 1, 3, [("Alice", 100)], **kw
        ),
        None,
    ),
    case(
        "price.vote",
        lambda **kw: price_embeds.build_vote_embed("Host", "scen", 1, 3, 20, **kw),
        None,
    ),
    case(
        "price.recap",
        lambda **kw: price_embeds.build_recap_embed("Host", 3, 4, {}, None, **kw),
        None,
    ),
    # Semantic green is this builder's *default*; the accent still wins when
    # passed (the original file asserted exactly this pair).
    case(
        "price.round_results",
        lambda **kw: price_embeds.build_round_results_embed(
            "Host", 1, 3, "A", 100, 1, "B", 500, 2, **kw
        ),
        discord.Color(services_embeds.COLOR_GREEN),
    ),
    # ── rushmore ─────────────────────────────────────────────────────────
    case(
        "rushmore.join",
        lambda **kw: rushmore_embeds.build_join_embed("Host", [], topic="Snacks", **kw),
        discord.Color(games_constants.PHASE_JOINING),
    ),
    case(
        "rushmore.draft",
        lambda **kw: rushmore_embeds.build_draft_embed(
            "Host", "Snacks", [(1, "Alice")], {"1": [None] * 4},
            active_player_id=1, active_player_name="Alice",
            round_num=1, timer_secs=30, **kw,
        ),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "rushmore.final_boards",
        lambda **kw: rushmore_embeds.build_final_boards_embed(
            "Host", "Snacks", [(1, "Alice")],
            {"1": ["Pizza", "Sushi", "Tacos", "Burgers"]}, **kw,
        ),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "rushmore.vote",
        lambda **kw: rushmore_embeds.build_vote_embed("Host", "Snacks", timer_secs=30, **kw),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "rushmore.recap",
        lambda **kw: rushmore_embeds.build_recap_embed(
            "Host", "Snacks", 3, 60.0, ["Alice"], 2, [["A", "B", "C", "D"]],
            stats={"skipped_count": 0}, **kw,
        ),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    # ── would you rather ─────────────────────────────────────────────────
    case(
        "wyr.round",
        lambda **kw: wyr_embeds.build_wyr_embed("Alice", "fly", "swim", [], [], False, 1, **kw),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "wyr.round_closed",
        lambda **kw: wyr_embeds.build_wyr_embed(
            "Alice", "fly", "swim", [], [], False, 1, closed=True, **kw
        ),
        None,
    ),
    case(
        "wyr.closed",
        lambda **kw: wyr_embeds.build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1, **kw),
        None,
    ),
    # ── two truths and a lie ─────────────────────────────────────────────
    case(
        "ttl.lobby",
        lambda **kw: ttl_embeds.build_lobby_embed(**kw),
        discord.Color(games_constants.PHASE_JOINING),
    ),
    case(
        "ttl.guess_open",
        lambda **kw: ttl_embeds.build_guess_embed("Alice", ["s1", "s2", "s3"], {}, **kw),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "ttl.guess_closed",
        lambda **kw: ttl_embeds.build_guess_embed(
            "Alice", ["s1", "s2", "s3"], {}, closed=True, **kw
        ),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "ttl.reveal",
        lambda **kw: ttl_embeds.build_reveal_embed(
            subject_name="Alice", statements=["t1", "t2", "LIE"], lie_index=2,
            correct_voters=[1], fooled_voters=[2], name_resolver=str, **kw,
        ),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    # build_recap_embed returns (embed, extra)
    case(
        "ttl.recap",
        lambda **kw: ttl_embeds.build_recap_embed({}, name_resolver=str, **kw)[0],
        discord.Color(games_constants.PHASE_RECAP),
    ),
    # ── hot takes ────────────────────────────────────────────────────────
    case(
        "hottakes.lobby",
        lambda **kw: hottakes_embeds.build_lobby_embed("Alice", **kw),
        discord.Color(games_constants.PHASE_JOINING),
    ),
    case(
        "hottakes.vote_open",
        lambda **kw: hottakes_embeds.build_vote_embed(
            "t", take_num=1, total_takes=1, votes_by_user={}, **kw
        ),
        discord.Color(games_constants.PHASE_PLAYING),
    ),
    case(
        "hottakes.vote_closed",
        lambda **kw: hottakes_embeds.build_vote_embed(
            "t", take_num=1, total_takes=1, votes_by_user={1: 4}, closed=True, **kw
        ),
        discord.Color(games_constants.PHASE_RESULTS),
    ),
    case(
        "hottakes.recap",
        lambda **kw: hottakes_embeds.build_recap_embed(
            [{"text": "x", "avg": 3.0, "std": 0.0, "voters": [1]}], **kw
        ),
        discord.Color(games_constants.PHASE_RECAP),
    ),
    # ── traditional / fantasies (brand-color fallback, not gray) ─────────
    case(
        "traditional.recap",
        lambda **kw: traditional_embeds.build_recap_embed(
            {"participants": [1], "asked": {}}, **kw
        ),
        discord.Color(games_constants.BRAND_COLOR),
    ),
    case(
        "fantasies.recap",
        lambda **kw: fantasies_embeds.build_recap_embed(
            [{"text": "x", "same_pct": 0.5, "voters": [1], "category": "Fantasy"}], **kw
        ),
        discord.Color(games_constants.BRAND_COLOR),
    ),
    # ── economy approval cards (accent is a required positional) ────────
    case(
        "economy.bounty_card_open",
        lambda **kw: economy_bounty_views.render_bounty_card(
            kw["color"],
            _econ_settings(),
            {
                "state": "open", "title": "Draw the mascot",
                "description": "Any medium.", "poster_id": 1, "winner_id": 2,
                "payout": 900, "rake_amount": 100,
            },
            pot=1000,
            contributors=3,
        ),
        None,
    ),
    case(
        "economy.pin_review_pending",
        lambda **kw: economy_pin_views.render_pin_review_embed(
            kw["color"],
            _econ_settings(),
            sponsor_mention="<@1>",
            message="Raid at 8pm.",
            price=300,
            state="pending",
            resolver_id=2,
            deny_reason="off-topic",
        ),
        None,
    ),
]

# Passthrough-only builders (fallback None) are excluded from the fallback test.
FALLBACK_CASES = [p for p in CASES if p.values[1] is not None]


@pytest.mark.parametrize(("build", "fallback"), CASES)
def test_builder_honors_passed_accent(build, fallback):
    assert build(color=ACCENT).color == ACCENT


@pytest.mark.parametrize(("build", "fallback"), FALLBACK_CASES)
def test_builder_falls_back_without_accent(build, fallback):
    assert build().color == fallback
