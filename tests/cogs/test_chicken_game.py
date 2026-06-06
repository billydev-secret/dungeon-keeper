"""Unit tests for chicken/game.py (pure logic, no Discord)."""
from __future__ import annotations

from bot_modules.cogs.chicken.game import (
    ChickenGame,
    bravest_bailer,
    game_from_row,
    meter_pct,
    resolve_crash,
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
    assert loser == 2           # deterministic (lowest id) crasher


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
