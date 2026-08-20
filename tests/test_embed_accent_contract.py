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

import importlib
import pkgutil
import re
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.cogs.casino import embeds as casino_embeds
from bot_modules.cogs.games_legitlibs import rendering as ll_rendering
from bot_modules.economy import bounty_views as economy_bounty_views
from bot_modules.services import economy_bounty_service
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
from bot_modules.music_playlist import embeds as music_playlist_embeds
from bot_modules.services import branding_service
from bot_modules.services import casino_service
from bot_modules.services import dm_branding
from bot_modules.services import economy_service
from bot_modules.services import event_echo_logic
from bot_modules.services import embeds as services_embeds
from bot_modules.services import pools_logic, pools_metrics
from bot_modules.services import welcome_service
from bot_modules.services.risky_roll import formatters as risky_formatters
from bot_modules.services.risky_roll import models as risky_models
from bot_modules.survivor import embeds as survivor_embeds
from bot_modules.survivor import reckoning as survivor_reckoning
from bot_modules.survivor.gauntlet import GauntletFate as _Fate
from bot_modules.survivor.gauntlet import ReplayWeek as _ReplayWeek
from bot_modules.survivor.logic import OpenGame as _SurvivorGame

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


def _welcome_member() -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.mention = "<@1>"
    member.display_name = "Alex"
    member.id = 1
    guild = MagicMock()
    guild.name = "Test Guild"
    guild.member_count = 42
    guild.icon = None
    member.guild = guild
    member.display_avatar.url = "https://cdn.example/a.png"
    return member


CASES = [
    case(
        "event_echo.game_starting",
        lambda **kw: event_echo_logic.build_echo_embed(
            name="Truth or Dare", channel_id=1, url="https://x/1", **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    # ── risky roll (accent param is named `accent`; accent=None IS the
    #    fallback path, so one call covers both harness invocations) ─────
    case(
        "risky.open_round",
        lambda **kw: risky_formatters.build_embed(_risky_state(), accent=kw.get("color")),
        discord.Color(0xDC3545),
    ),
    case(
        "risky.round_over",
        lambda **kw: risky_formatters.build_embed(
            _risky_state(rolls={1: 90, 2: 5}, highest_user=1, lowest_user=2, is_open=False),
            accent=kw.get("color"),
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
    # ── casino (fallback is the house gold; older casino builders are
    # ledger debt below — new ones land here) ────────────────────────────
    case(
        "casino.baccarat_round",
        lambda **kw: casino_embeds.build_baccarat_round_embed(
            _econ_settings(), 0.0, [], kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.baccarat_deal",
        lambda **kw: casino_embeds.build_baccarat_deal_embed(
            _econ_settings(), ["A♠", "8♦"], ["K♠", "Q♦"], kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.dice_round",
        lambda **kw: casino_embeds.build_dice_round_embed(
            _econ_settings(), 0.0, [], kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.dice_tumble",
        lambda **kw: casino_embeds.build_dice_tumble_embed(
            _econ_settings(), kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        # The live standoff (outcome=None) is the accent-colored state;
        # win/lose verdicts are semantic and tested in the embeds file.
        "casino.war_standoff",
        lambda **kw: casino_embeds.build_war_embed(
            _econ_settings(), 1, "7♠", "7♦", 10, kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        # The live grid (outcome=None) is the accent-colored state; the
        # cashed/bombed verdicts are semantic and tested in the embeds file.
        "casino.mines_grid",
        lambda **kw: casino_embeds.build_mines_embed(
            _econ_settings(), 1,
            casino_service.MinesStep(hand_id=1, stake=10, bombs=3, revealed=(4,),
                                     mult=133, next_mult=159),
            kw.get("color"),
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.keno_round",
        lambda **kw: casino_embeds.build_keno_round_embed(
            _econ_settings(), 0.0, [], kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.keno_tumble",
        lambda **kw: casino_embeds.build_keno_tumble_embed(
            _econ_settings(), kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.pools_panel",
        lambda **kw: casino_embeds.build_pools_panel_embed(
            _econ_settings(), 3904.5, pools_logic.PoolSplit(0, 0), 0.0,
            "2026-07-20", kw.get("color"),
            spec=pools_metrics.SPECS[pools_metrics.ANCHOR],
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.pools_result",
        lambda **kw: casino_embeds.build_pools_result_embed(
            _econ_settings(), "2026-07-20", 5000, 3904.5, pools_logic.OVER,
            [], 19, kw.get("color"),
            spec=pools_metrics.SPECS[pools_metrics.ANCHOR],
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    case(
        "casino.pools_void",
        lambda **kw: casino_embeds.build_pools_void_embed(
            "2026-07-20", 250, kw.get("color")
        ),
        discord.Color(services_embeds.COLOR_GOLD),
    ),
    # ── welcome / leave (member arg is constructed mock data) ───────────
    case(
        "welcome.join",
        lambda **kw: welcome_service.build_welcome_embed(
            _welcome_member(), "hi {member}", **kw
        ),
        discord.Color.blurple(),
    ),
    case(
        "welcome.leave",
        lambda **kw: welcome_service.build_leave_embed(
            _welcome_member(), "bye {member_name}", **kw
        ),
        None,
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
        "economy.bounty_hub_panel",
        lambda **kw: economy_bounty_views.build_bounty_hub_embed(
            kw["color"],
            _econ_settings(),
            123,
            [
                economy_bounty_service.BountyBoardEntry(
                    bounty_id=1, title="Draw the mascot", pot=1000,
                    contributors=3, card_channel_id=7, card_message_id=8,
                )
            ],
            open_total=1,
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
    case(
        "dm_branding.branded_dm",
        lambda **kw: dm_branding.brand_dm_embed(
            discord.Embed(title="A DM"), guild_name="Test Guild", **kw
        ),
        discord.Color(services_embeds.DM_PRIMARY),
    ),
    # ── survivor ─────────────────────────────────────────────────────────
    case(
        "survivor.status",
        lambda **kw: survivor_embeds.build_status_embed(
            _survivor_status(), season_name="S", **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.pick_confirm",
        lambda **kw: survivor_embeds.build_pick_confirm_embed(
            _SurvivorGame(
                team="SEA", opponent="NE", is_home=True, game_id="g",
                week=1, kickoff_ts=1_700_000_000.0,
            ),
            _survivor_status(), changed=False, **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.board",
        lambda **kw: survivor_embeds.build_board_embed(
            {
                "week": 1,
                "alive": [{"user_id": 1, "strikes_used": 0, "weeks_survived": 2}],
                "graveyard": [{"user_id": 2, "eliminated_week": 1}],
                "pots": {"main": 8000, "ghost": 2000},
                "most_burned": [("SEA", 2)],
            },
            lambda uid: f"soul {uid}", season_name="S", **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.reckoning",
        lambda **kw: survivor_reckoning.build_reckoning_embed(
            {
                "week": 1, "before": 3, "after": 2,
                "pots": {"main": 8000, "ghost": 2000},
                "toll_line": "the toll", "stragglers": 0,
                "arrivals": [], "eulogy_lines": ["{name} fell."],
                "deaths": [{"user_id": 2, "teams": "NE 💀", "state": "💀",
                            "died": True, "source": "picks",
                            "fatal_team": "NE"}],
                "ledger": [{"user_id": 1, "teams": "SEA ✅", "state": "",
                            "died": False, "source": None,
                            "fatal_team": None}],
                "streak_strip": [], "streak_record": 0,
            },
            lambda uid: f"soul {uid}", season_name="S", **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.history",
        lambda **kw: survivor_embeds.build_history_embed(
            [{"week": 1, "team": "SEA", "result": "win",
              "auto_assigned": False}],
            display_name="Loaf", revealed_week=1, own=False, **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.gauntlet_receipt",
        lambda **kw: survivor_embeds.build_gauntlet_receipt_embed(
            _Fate(
                weeks=(_ReplayWeek(1, "KC", "g1", "win", 0, False),),
                elapsed_count=1, strikes_used=0, dead=False,
                death_week=None, burned=("KC",), fee=50,
            ),
            buyin=0, **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.panel_enrolling",
        lambda **kw: survivor_embeds.build_panel_embed(
            season_name="S", entrants=3, buyin=0, gauntlet_mode=False, **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.panel_active",
        lambda **kw: survivor_embeds.build_panel_embed(
            season_name="S", entrants=9, buyin=0, gauntlet_mode=True,
            week=3, games=[{"home": "SEA", "away": "NE",
                            "kickoff_ts": 1_700_000_000.0}],
            alive=7, eliminated=2, picked=5, pot=8150, ghost_pot=2000, **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    # ── music playlist (passthrough-only: `color` is a required keyword) ─
    case(
        "music_playlist.window",
        lambda **kw: music_playlist_embeds.build_window_embed(
            [], window_size=30, color=kw["color"]
        ),
        None,
    ),
    case(
        "music_playlist.my_posts",
        lambda **kw: music_playlist_embeds.build_my_posts_embed(
            [], window_size=30, color=kw["color"]
        ),
        None,
    ),
    case(
        "music_playlist.my_review",
        lambda **kw: music_playlist_embeds.build_my_review_embed([], color=kw["color"]),
        None,
    ),
]


def _survivor_status() -> dict:
    return {
        "status": "alive", "strikes_used": 0, "strikes_allowed": 1,
        "eliminated_week": None, "week": 1,
        "pick": {"team": "SEA", "game_id": "g", "auto_assigned": False,
                 "result": None},
        "pick_locked": False, "pick_kickoff_ts": 1_700_000_000.0,
        "satchel_count": 31, "satchel_low": False,
    }

# Passthrough-only builders (fallback None) are excluded from the fallback test.
FALLBACK_CASES = [p for p in CASES if p.values[1] is not None]


@pytest.mark.parametrize(("build", "fallback"), CASES)
def test_builder_honors_passed_accent(build, fallback):
    assert build(color=ACCENT).color == ACCENT


@pytest.mark.parametrize(("build", "fallback"), FALLBACK_CASES)
def test_builder_falls_back_without_accent(build, fallback):
    assert build().color == fallback


# ── completeness guard ───────────────────────────────────────────────────
# Without this, the table is documentation: a new embeds module could ship
# builders with zero accent coverage while every test stays green. Discovery
# walks the embeds/rendering/formatters modules; every builder must appear in
# a case() above or in the ledger below. The ledger is the debt as of
# 2026-07-25 — move entries into CASES over time; never add to it silently
# (adding an entry is an explicit, reviewable decision to skip the contract,
# e.g. for content-string builders or semantic-color-only surfaces).
KNOWN_UNCOVERED = {
    # Bios carries its own per-guild dial (`bios_embed_color`, BiosConfig) and
    # takes it as a required `embed_color=` argument rather than the optional
    # `color=` the contract probes — there is no accent path to pass through and
    # no fallback to assert. The wizard's two prompt embeds are the same builder
    # family as the posted bio and share that dial.
    "bot_modules.bios.embeds.build_bio_embed",
    "bot_modules.bios.embeds.build_field_prompt_embed",
    "bot_modules.bios.embeds.build_question_prompt_embed",
    # baccarat result is semantic green/red only; the running note is a
    # content string — both out of accent scope by design.
    "bot_modules.cogs.casino.embeds.build_baccarat_result_embed",
    # The big-win broadcast takes no color argument at all: it copies the
    # color off the result card it is built from, which a broadcast only ever
    # reaches on a win, so it is always the semantic green.
    "bot_modules.cogs.casino.embeds.build_big_win_broadcast",
    "bot_modules.cogs.casino.embeds.build_coup_running_note",
    "bot_modules.cogs.casino.embeds.build_dice_result_embed",
    "bot_modules.cogs.casino.embeds.build_draw_running_note",
    "bot_modules.cogs.casino.embeds.build_keno_result_embed",
    "bot_modules.cogs.casino.embeds.build_roll_running_note",
    "bot_modules.cogs.casino.embeds.build_blackjack_embed",
    "bot_modules.cogs.casino.embeds.build_blackjack_reveal_embed",
    "bot_modules.cogs.casino.embeds.build_coinflip_embed",
    "bot_modules.cogs.casino.embeds.build_coinflip_spin_embed",
    "bot_modules.cogs.casino.embeds.build_derby_race_embed",
    "bot_modules.cogs.casino.embeds.build_derby_result_embed",
    "bot_modules.cogs.casino.embeds.build_derby_round_embed",
    "bot_modules.cogs.casino.embeds.build_help_embed",
    "bot_modules.cogs.casino.embeds.build_hub_embed",
    "bot_modules.cogs.casino.embeds.build_jackpot_celebration",
    "bot_modules.cogs.casino.embeds.build_my_stats_embed",
    "bot_modules.cogs.casino.embeds.build_race_running_note",
    "bot_modules.cogs.casino.embeds.build_roulette_bounce_embed",
    "bot_modules.cogs.casino.embeds.build_roulette_result_embed",
    "bot_modules.cogs.casino.embeds.build_roulette_round_embed",
    "bot_modules.cogs.casino.embeds.build_round_running_note",
    "bot_modules.cogs.casino.embeds.build_slots_embed",
    "bot_modules.cogs.casino.embeds.build_slots_spin_embed",
    "bot_modules.cogs.games_legitlibs.rendering.render_filled_body",
    "bot_modules.cogs.games_legitlibs.rendering.render_filled_body_attributed",
    "bot_modules.cogs.games_legitlibs.rendering.render_redacted_body",
    "bot_modules.dm_perms.embeds.build_acceptance_embed",
    "bot_modules.dm_perms.embeds.build_denial_embed_for_requester",
    "bot_modules.dm_perms.embeds.build_denial_embed_for_view",
    "bot_modules.dm_perms.embeds.build_dm_settings_embed",
    "bot_modules.dm_perms.embeds.build_expired_embed",
    "bot_modules.dm_perms.embeds.build_guild_unavailable_embed",
    "bot_modules.dm_perms.embeds.build_request_dm_embed",
    "bot_modules.dm_perms.embeds.build_request_sent_embed",
    "bot_modules.dm_perms.embeds.build_revoked_embed",
    "bot_modules.dm_perms.embeds.build_stale_request_embed",
    "bot_modules.games_ama.embeds.build_answered_embed",
    "bot_modules.games_ama.embeds.build_asker_dm_embed",
    "bot_modules.games_ama.embeds.build_main_embed",
    "bot_modules.games_ama.embeds.build_panel_embed",
    "bot_modules.games_ama.embeds.build_question_embed",
    "bot_modules.games_compliment.embeds.build_pairings_embed",
    "bot_modules.games_config.embeds.build_audit_channel_embed",
    "bot_modules.games_config.embeds.build_channel_allowed_embed",
    "bot_modules.games_config.embeds.build_channel_disallowed_embed",
    "bot_modules.games_config.embeds.build_channel_list_embed",
    "bot_modules.games_config.embeds.build_force_end_embed",
    "bot_modules.games_config.embeds.build_game_status_embed",
    "bot_modules.games_fantasies.embeds.build_round_submit_embed",
    "bot_modules.games_help.embeds.build_help_embed",
    "bot_modules.games_help.embeds.build_support_embed",
    "bot_modules.games_mfk.embeds.build_assignments_embed",
    "bot_modules.games_rushmore.embeds.build_winner_embed",
    "bot_modules.games_rushmore.embeds.render_draft_board",
    "bot_modules.games_session.embeds.build_session_recap_embed",
    "bot_modules.games_story.embeds.build_attribution_embed",
    "bot_modules.games_story.embeds.build_complete_story_embed",
    "bot_modules.games_story.embeds.build_turn_embed",
    "bot_modules.games_traditional.embeds.build_question_embed",
    "bot_modules.games_traditional.embeds.build_tod_embed",
    "bot_modules.jail.embeds.build_adopted_policies_embed",
    "bot_modules.jail.embeds.build_jail_audit_embed",
    "bot_modules.jail.embeds.build_modinfo_embed",
    "bot_modules.jail.embeds.build_policy_close_embed",
    "bot_modules.jail.embeds.build_policy_list_embed",
    "bot_modules.jail.embeds.build_policy_proposal_embed",
    "bot_modules.jail.embeds.build_policy_vote_initial_embed",
    "bot_modules.jail.embeds.build_policy_vote_update_embed",
    "bot_modules.jail.embeds.build_ticket_open_embed",
    "bot_modules.jail.embeds.build_ticket_panel_embed",
    "bot_modules.jail.embeds.build_warning_audit_embed",
    "bot_modules.jail.embeds.build_warning_revoke_audit_embed",
    "bot_modules.jail.embeds.build_warning_threshold_embed",
    "bot_modules.music.embeds.build_queue_embed",
    "bot_modules.services.risky_roll.formatters.build_how_to_play_content",
    "bot_modules.services.risky_roll.formatters.build_pending_prompt_content",
    "bot_modules.services.risky_roll.formatters.build_pending_question_summary",
    "bot_modules.services.risky_roll.formatters.build_question_reply_content",
    "bot_modules.services.risky_roll.formatters.build_rolloff_embed",
    "bot_modules.services.embeds.build_admin_mirror_embed",
    "bot_modules.starboard.embeds.build_starboard_embed",
    "bot_modules.voice_master.embeds.build_claim_done_embed",
    "bot_modules.voice_master.embeds.build_claim_prompt_embed",
    "bot_modules.voice_master.embeds.build_howto_embed",
    "bot_modules.voice_master.embeds.build_inline_panel_embed",
    "bot_modules.voice_master.embeds.build_knock_request_embed",
    "bot_modules.voice_master.embeds.build_panel_embed",
    "bot_modules.voice_master.embeds.build_profile_show_embed",
    "bot_modules.whisper.embeds.build_inbox_embed",
    "bot_modules.whisper.embeds.build_reply_audit_embed",
    "bot_modules.whisper.embeds.build_reply_report_audit_embed",
    "bot_modules.whisper.embeds.build_report_audit_embed",
    "bot_modules.whisper.embeds.build_send_feed_embed",
    "bot_modules.whisper.embeds.build_share_feed_embed",
}


def _discover_builders() -> set[str]:
    """Every build_*/render_* callable in an embeds/rendering/formatters module."""
    import bot_modules

    found: set[str] = set()
    for info in pkgutil.walk_packages(bot_modules.__path__, "bot_modules."):
        if info.name.rsplit(".", 1)[-1] not in ("embeds", "rendering", "formatters"):
            continue
        module = importlib.import_module(info.name)
        for attr, obj in vars(module).items():
            if (
                callable(obj)
                and getattr(obj, "__module__", None) == info.name
                and attr.startswith(("build_", "render_"))
            ):
                found.add(f"{info.name}.{attr}")
    return found


def test_every_builder_is_under_contract_or_explicitly_exempt():
    # A builder counts as covered if its (unqualified) name is invoked
    # somewhere in this file — i.e. it has a case() row. Ledger entries are
    # fully-qualified and never followed by "(", so they don't self-match.
    source = Path(__file__).read_text(encoding="utf-8")
    covered = set(re.findall(r"\.((?:build|render)_\w+)\(", source))
    discovered = _discover_builders()

    missing = sorted(
        b for b in discovered
        if b.rsplit(".", 1)[-1] not in covered and b not in KNOWN_UNCOVERED
    )
    assert not missing, (
        "New embed builder(s) with no accent-contract coverage. Add a case() "
        "row above (preferred) or an explicit KNOWN_UNCOVERED entry:\n  "
        + "\n  ".join(missing)
    )

    stale = sorted(KNOWN_UNCOVERED - discovered)
    assert not stale, (
        "KNOWN_UNCOVERED entries that no longer exist (or moved) — remove "
        "them so the ledger stays honest:\n  " + "\n  ".join(stale)
    )
