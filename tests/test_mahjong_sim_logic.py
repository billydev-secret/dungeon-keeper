"""Tests for the headless card simulator — stage 2 of
docs/plans/mahjong-card-generator.md.

Every case runs a **two-line card for two games**. That is deliberate: one
game of the shipped 22-hand card costs seconds of bot thinking (the assist
engine walks every line's bindings at every decision), and this file runs in
the scoped pre-commit gate. Two lines keeps the binding enumeration small
enough that the whole module lands well under a second, while still
exercising the real engine and the real brain end to end.

The worker-count guarantee is checked *without* spawning a process pool:
sharding is `_run_shard` over disjoint index ranges plus `merge_into`, and
that composition is what the pool parallelises. Testing it directly gets the
same assurance without putting multiprocessing (and its spawn-vs-fork
platform differences) inside CI.
"""

from __future__ import annotations

import pytest

from bot_modules.games.mahjong.card_logic import load_card, load_first_light
from bot_modules.games.mahjong.game_logic import TableConfig
from bot_modules.games.mahjong.sim_logic import (
    HandStat,
    SimReport,
    _empty_report,
    _rng_for,
    _run_shard,
    demand_spread,
    format_report,
    merge_into,
    simulate,
)


def _card(*hands):
    return load_card({
        "card_id": "sim-test", "display_name": "Sim Test", "season": "t",
        "hands": list(hands),
    })


#: Two ordinary, comfortably reachable lines — an all-evens run and a winds
#: hand, sharing no tiles, so both are live from most deals.
TINY = _card(
    {"id": "evens", "section": "Numbers", "name": "Evens",
     "concealed": False, "value": 25,
     "groups": [{"count": 2, "rank": "F"},
                {"count": 4, "rank": "2", "suit": "a"},
                {"count": 4, "rank": "4", "suit": "a"},
                {"count": 4, "rank": "6", "suit": "a"}]},
    {"id": "winds", "section": "Honors", "name": "Winds",
     "concealed": False, "value": 30,
     "groups": [{"count": 4, "rank": "N"}, {"count": 3, "rank": "E"},
                {"count": 3, "rank": "W"}, {"count": 4, "rank": "S"}]},
)

#: Lint-clean but brutal: fourteen tiles of concealed singles and pairs
#: spread over all three suits, where no joker may stand anywhere. Nobody
#: completes this in a couple of games — the "dead ink" case.
BRUTAL = _card(
    {"id": "impossible", "section": "Hard", "name": "Needle",
     "concealed": True, "value": 75,
     "groups": [{"count": 2, "rank": "1", "suit": "a"},
                {"count": 2, "rank": "5", "suit": "a"},
                {"count": 2, "rank": "9", "suit": "a"},
                {"count": 2, "rank": "1", "suit": "b"},
                {"count": 2, "rank": "5", "suit": "b"},
                {"count": 2, "rank": "9", "suit": "b"},
                {"count": 1, "rank": "1", "suit": "c"},
                {"count": 1, "rank": "9", "suit": "c"}]},
)


def _fingerprint(report: SimReport):
    """Everything measured, as a comparable value."""
    return (
        report.mahjongs, report.wall_games, report.fallow_ends,
        report.other_ends, report.total_turns, report.rejected_actions,
        report.stuck_games,
        {h.hand_id: (h.targeted, h.wins, h.win_turns, h.jokerless_wins)
         for h in report.hands.values()},
    )


# ── The engine actually runs ─────────────────────────────────────────────────


def test_a_run_plays_real_games_and_stays_healthy():
    report = simulate(TINY, games=2, seed=1)
    assert report.games == 2
    assert report.total_turns > 0, "no discard was ever made"
    assert report.healthy, "a bot action was refused or a table stuck"


@pytest.mark.parametrize("seat_count", [2, 4])
def test_every_game_ends_in_exactly_one_outcome(seat_count):
    report = simulate(TINY, games=2, seat_count=seat_count, seed=4)
    ends = (
        report.mahjongs + report.wall_games
        + report.fallow_ends + report.other_ends
    )
    assert ends == report.games
    assert report.seat_count == seat_count


def test_hands_are_seeded_from_the_card_in_card_order():
    report = simulate(TINY, games=1, seed=0)
    assert list(report.hands) == ["evens", "winds"]
    assert report.hands["winds"].value == 30
    assert report.hands["winds"].section == "Honors"


def test_opening_targets_never_exceed_one_per_seat_per_game():
    report = simulate(TINY, games=2, seat_count=4, seed=5)
    assert sum(h.targeted for h in report.hands.values()) <= 2 * 4


# ── Determinism (G4) ─────────────────────────────────────────────────────────


def test_same_seed_gives_an_identical_report():
    assert _fingerprint(simulate(TINY, games=2, seed=11)) == _fingerprint(
        simulate(TINY, games=2, seed=11)
    )


def test_a_different_seed_plays_different_games():
    a = simulate(TINY, games=2, seed=11)
    b = simulate(TINY, games=2, seed=12)
    assert a.total_turns != b.total_turns or _fingerprint(a) != _fingerprint(b)


def test_rng_streams_are_per_game_and_distinct():
    assert _rng_for(3, 0).random() != _rng_for(3, 1).random()
    assert _rng_for(3, 7).random() == _rng_for(3, 7).random()


def test_sharding_and_merging_reproduce_the_serial_run():
    """The worker-count guarantee, without a process pool: games 0–1 and 2–3
    computed separately then merged must equal games 0–3 run in one go."""
    serial = simulate(TINY, games=4, seed=9)

    config = TableConfig(seat_count=4, wall_trim=0, second_charleston=True)
    merged = _empty_report(TINY, 4, 4, 9)
    for start, stop in ((0, 2), (2, 4)):
        merge_into(merged, _run_shard((TINY, config, 9, start, stop)))

    assert _fingerprint(merged) == _fingerprint(serial)


def test_merge_into_sums_every_measured_field():
    a = _empty_report(TINY, 1, 4, 0)
    b = _empty_report(TINY, 1, 4, 0)
    a.mahjongs, a.wall_games, a.total_turns, a.stuck_games = 1, 2, 30, 1
    b.mahjongs, b.wall_games, b.total_turns, b.rejected_actions = 3, 4, 70, 2
    a.hands["evens"].targeted, a.hands["evens"].wins = 5, 1
    b.hands["evens"].targeted, b.hands["evens"].jokerless_wins = 6, 1
    merge_into(a, b)
    assert (a.mahjongs, a.wall_games, a.total_turns) == (4, 6, 100)
    assert (a.stuck_games, a.rejected_actions) == (1, 2)
    assert (a.hands["evens"].targeted, a.hands["evens"].wins) == (11, 1)
    assert a.hands["evens"].jokerless_wins == 1


# ── A card that cannot be won is reported, not hung ──────────────────────────


def test_an_unwinnable_card_walls_out_instead_of_hanging():
    report = simulate(BRUTAL, games=2, seed=2)
    assert report.mahjongs == 0
    assert report.wall_games == 2
    assert report.healthy, "the table must still terminate cleanly"
    assert [h.hand_id for h in report.dead_lines] == ["impossible"]


def test_dead_and_never_targeted_are_different_questions():
    report = simulate(BRUTAL, games=2, seed=2)
    # the only line is dead, but seats still opened aiming at it — a line
    # can be attractive and unwinnable at once, which is the failure mode
    # the generator most needs to see
    assert report.dead_lines
    assert not report.never_targeted


# ── Rates and guards ─────────────────────────────────────────────────────────


def test_rates_are_derived_from_the_counts():
    report = _empty_report(TINY, 4, 4, 0)
    report.mahjongs, report.wall_games, report.total_turns = 1, 3, 200
    assert report.win_rate == 0.25
    assert report.wall_game_rate == 0.75
    assert report.mean_turns == 50


def test_wins_per_target_may_exceed_one_when_seats_pivot():
    stat = HandStat(hand_id="x", section="s", name="n", value=25, concealed=False)
    stat.targeted, stat.wins = 2, 5
    assert stat.wins_per_target == 2.5
    stat.targeted = 0
    assert stat.wins_per_target is None


def test_jokerless_rate_is_none_before_any_win():
    stat = HandStat(hand_id="x", section="s", name="n", value=25, concealed=False)
    assert stat.jokerless_rate is None
    stat.wins, stat.jokerless_wins = 4, 1
    assert stat.jokerless_rate == 0.25


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        pytest.param({"games": 0}, "games must be", id="no-games"),
        pytest.param({"games": 1, "workers": 0}, "workers must be", id="no-workers"),
    ],
)
def test_bad_arguments_are_refused(kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        simulate(TINY, **kwargs)


def test_health_is_false_when_the_engine_refused_a_bot():
    report = _empty_report(TINY, 1, 4, 0)
    assert report.healthy
    report.rejected_actions = 1
    assert not report.healthy
    report.rejected_actions, report.stuck_games = 0, 1
    assert not report.healthy


# ── Presentation ─────────────────────────────────────────────────────────────


def test_format_report_lists_lines_worst_first_and_names_the_dead():
    report = _empty_report(TINY, 2, 4, 0)
    report.hands["winds"].targeted, report.hands["winds"].wins = 3, 2
    report.hands["evens"].targeted = 9
    text = format_report(report)
    assert text.index("evens") < text.index("winds"), "dead line must sort first"
    assert "1 line(s) never won: evens" in text


def test_format_report_shouts_when_the_run_is_untrustworthy():
    report = _empty_report(TINY, 1, 4, 0)
    report.stuck_games = 1
    assert "UNHEALTHY" in format_report(report)
    assert "UNHEALTHY" not in format_report(_empty_report(TINY, 1, 4, 0))


def test_demand_spread_counts_lines_per_rank_token():
    spread = demand_spread(TINY)
    assert spread["F"] == 1      # only the evens line wants flowers
    assert spread["N"] == 1
    assert spread["2"] == 1
    # a token repeated inside one hand still counts that hand once
    assert max(spread.values()) == 1


def test_demand_spread_sees_first_light_leaning_on_flowers():
    spread = demand_spread(load_first_light())
    assert spread["F"] > spread["N"], "First Light is a flower-heavy card"
