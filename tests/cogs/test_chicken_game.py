"""Unit tests for chicken/game.py (pure logic, no Discord)."""
from __future__ import annotations

import random

import pytest

from bot_modules.cogs.chicken.game import (
    ChickenGame,
    bravest_bailer,
    crash_pct,
    game_from_row,
    meter_pct,
    resolve_crash,
    roll_crash_at,
)


# ── meter_pct ──────────────────────────────────────────────────────────────────

def test_meter_pct_progression():
    assert meter_pct(0.0, 0.0, 20.0) == 0.0
    assert meter_pct(10.0, 0.0, 20.0) == 50.0
    assert meter_pct(20.0, 0.0, 20.0) == 100.0


def test_meter_pct_clamped():
    assert meter_pct(30.0, 0.0, 20.0) == 100.0
    assert meter_pct(-5.0, 0.0, 20.0) == 0.0


def test_meter_pct_no_start_or_duration():
    assert meter_pct(5.0, None, 20.0) == 0.0
    assert meter_pct(5.0, 0.0, None) == 0.0
    assert meter_pct(5.0, 0.0, 0.0) == 0.0


# ── bravest_bailer ─────────────────────────────────────────────────────────────

def test_bravest_bailer_highest_pct():
    log = [
        {"player_id": 1, "bail_ts": 1.0, "meter_pct": 30.0},
        {"player_id": 2, "bail_ts": 2.0, "meter_pct": 80.0},
        {"player_id": 3, "bail_ts": 3.0, "meter_pct": 55.0},
    ]
    assert bravest_bailer(log)["player_id"] == 2


def test_bravest_bailer_empty():
    assert bravest_bailer([]) is None


# ── resolve_crash ──────────────────────────────────────────────────────────────

def test_resolve_crash_with_bailers_and_crashers():
    bail = [{"player_id": 9, "bail_ts": 1.0, "meter_pct": 70.0}]
    winner, loser = resolve_crash([2, 5], bail)
    assert winner == 9          # bravest bailer
    assert loser in (2, 5)      # one crasher, drawn at random


def test_resolve_crash_tie_break_is_random_not_lowest_id():
    """duels-party-123: the loser used to be min(crashers), so the oldest
    account at the table ate the nickname every single time. The draw is
    seeded, so the same seed replays the same loser and different seeds
    reach every crasher."""
    bail = [{"player_id": 9, "bail_ts": 1.0, "meter_pct": 70.0}]
    crashers = [2, 5, 7]
    first = resolve_crash(crashers, bail, random.Random(1234))
    assert first == resolve_crash(crashers, bail, random.Random(1234))
    seen = {resolve_crash(crashers, bail, random.Random(seed))[1] for seed in range(40)}
    assert seen == set(crashers)
    assert first[1] in crashers


def test_resolve_crash_total_wipeout_no_bailers():
    winner, loser = resolve_crash([1, 2, 3], [])
    assert winner is None
    assert loser is None


def test_resolve_crash_picks_best_bailer_as_winner():
    bail = [
        {"player_id": 1, "bail_ts": 1.0, "meter_pct": 20.0},
        {"player_id": 2, "bail_ts": 2.0, "meter_pct": 90.0},
    ]
    winner, loser = resolve_crash([7], bail)
    assert winner == 2
    assert loser == 7


# ── roll_crash_at / crash_pct ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("lo", "hi", "seed", "expected"),
    [
        pytest.param(10.0, 25.0, 7, random.Random(7).uniform(10.0, 25.0), id="seeded"),
        pytest.param(25.0, 25.0, 1, 25.0, id="degenerate-range"),
        pytest.param(30.0, 20.0, 1, 30.0, id="ceiling-below-floor-lifted"),
        pytest.param(-5.0, 0.0, 1, 0.0, id="negative-floor-clamped"),
    ],
)
def test_roll_crash_at(lo, hi, seed, expected):
    assert roll_crash_at(lo, hi, random.Random(seed)) == pytest.approx(expected)


def test_roll_crash_at_covers_the_whole_range():
    """duels-party-113: the crash used to be fixed at the end of the climb.
    Rolled over many seeds it has to land well short of the ceiling too."""
    rolls = [roll_crash_at(10.0, 25.0, random.Random(s)) for s in range(200)]
    assert all(10.0 <= r <= 25.0 for r in rolls)
    assert min(rolls) < 13.0 and max(rolls) > 22.0


@pytest.mark.parametrize(
    ("crash_at", "duration", "expected"),
    [
        pytest.param(15.0, 25.0, 60.0, id="mid-bar"),
        pytest.param(25.0, 25.0, 100.0, id="at-ceiling"),
        pytest.param(None, 25.0, 100.0, id="pre-migration-row"),
        pytest.param(15.0, 0.0, 100.0, id="no-span"),
    ],
)
def test_crash_pct(crash_at, duration, expected):
    assert crash_pct(crash_at, duration) == pytest.approx(expected)


# ── game_from_row / dataclass ──────────────────────────────────────────────────

def test_challenger_id_aliases_host_id():
    g = ChickenGame(id=1, guild_id=1, channel_id=1, host_id=55, state="LOBBY")
    assert g.challenger_id == 55


def _row(**kwargs):
    defaults = dict(
        id=1, guild_id=100, channel_id=200, host_id=10, state="LOBBY",
        phase=None, roster="[10]", alive="[]", elimination_order="[]", bail_log="[]",
        winner_id=None, loser_id=None, stakes_text=None,
        message_id=None, result_message_id=None,
        climb_started_at=None, climb_duration=None,
        last_action_at=None, resolved_at=None, created_at=1000.0,
    )
    defaults.update(kwargs)
    return defaults


def test_game_from_row_parses_bail_log():
    g = game_from_row(_row(alive="[1,2]", bail_log='[{"player_id": 3, "bail_ts": 1.0, "meter_pct": 50.0}]'))
    assert g.alive == [1, 2]
    assert len(g.bail_log) == 1
    assert g.bail_log[0]["player_id"] == 3


def test_game_from_row_null_json_empty():
    g = game_from_row(_row(alive=None, bail_log=None, roster=None))
    assert g.alive == [] and g.bail_log == [] and g.roster == []


def test_game_from_row_crash_at_is_optional():
    """A row fetched before migration 208 has no crash_at column at all."""
    assert game_from_row(_row()).crash_at is None
    assert game_from_row(_row(crash_at=12.5)).crash_at == 12.5
