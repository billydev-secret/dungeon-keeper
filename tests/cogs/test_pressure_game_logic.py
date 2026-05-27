"""Unit tests for pressure_cooker/game.py — pure logic, no Discord."""
from __future__ import annotations

import json
import time

import pytest

from bot_modules.cogs.pressure_cooker.game import (
    GAUGE_CEILING,
    ROLL_MAX,
    ROLL_MIN,
    PressureGame,
    PumpEntry,
    apply_pump,
    game_from_row,
    pumps_to_json,
    roll_pump,
)


def _make_game(
    *,
    gauge: int = 0,
    state: str = "ACTIVE",
    active_player: int = 1,
    challenger_id: int = 1,
    target_id: int = 2,
) -> PressureGame:
    return PressureGame(
        id=1,
        guild_id=9001,
        channel_id=100,
        challenger_id=challenger_id,
        target_id=target_id,
        state=state,
        gauge=gauge,
        active_player=active_player,
    )


# ── roll_pump ────────────────────────────────────────────────────────────────

def test_roll_pump_always_in_range():
    samples = [roll_pump() for _ in range(1000)]
    assert all(ROLL_MIN <= s <= ROLL_MAX for s in samples), "roll out of [1,15]"


def test_roll_pump_uses_full_range():
    samples = set(roll_pump() for _ in range(2000))
    assert samples == set(range(ROLL_MIN, ROLL_MAX + 1))


# ── apply_pump basic ─────────────────────────────────────────────────────────

def test_apply_pump_advances_gauge():
    game = _make_game(gauge=20)
    result = apply_pump(game, player_id=1, roll=7)
    assert result.gauge_before == 20
    assert result.gauge_after == 27
    assert game.gauge == 27


def test_apply_pump_switches_turn_challenger_to_target():
    game = _make_game(active_player=1, challenger_id=1, target_id=2)
    apply_pump(game, player_id=1, roll=5)
    assert game.active_player == 2


def test_apply_pump_switches_turn_target_to_challenger():
    game = _make_game(active_player=2, challenger_id=1, target_id=2, gauge=10)
    apply_pump(game, player_id=2, roll=5)
    assert game.active_player == 1


def test_apply_pump_records_entry():
    game = _make_game(gauge=0)
    apply_pump(game, player_id=1, roll=10)
    assert len(game.pumps) == 1
    assert game.pumps[0].roll == 10
    assert game.pumps[0].player_id == 1
    assert game.pumps[0].gauge_before == 0


def test_apply_pump_sets_last_pump_at():
    game = _make_game()
    before = time.time()
    apply_pump(game, player_id=1, roll=5)
    assert game.last_pump_at is not None
    assert game.last_pump_at >= before


# ── bust ─────────────────────────────────────────────────────────────────────

def test_apply_pump_bust_sets_state_resolved():
    game = _make_game(gauge=90, active_player=1, challenger_id=1, target_id=2)
    result = apply_pump(game, player_id=1, roll=15)
    assert result.busted is True
    assert game.state == "RESOLVED"


def test_apply_pump_bust_sets_winner_and_loser():
    game = _make_game(gauge=90, active_player=1, challenger_id=1, target_id=2)
    result = apply_pump(game, player_id=1, roll=15)
    assert result.loser_id == 1
    assert result.winner_id == 2
    assert game.loser_id == 1
    assert game.winner_id == 2


def test_apply_pump_bust_target_loses():
    game = _make_game(gauge=90, active_player=2, challenger_id=1, target_id=2)
    result = apply_pump(game, player_id=2, roll=15)
    assert result.loser_id == 2
    assert result.winner_id == 1


def test_apply_pump_bust_sets_resolved_at():
    game = _make_game(gauge=90)
    before = time.time()
    apply_pump(game, player_id=1, roll=15)
    assert game.resolved_at is not None
    assert game.resolved_at >= before


def test_apply_pump_no_bust_result_fields():
    game = _make_game(gauge=0)
    result = apply_pump(game, player_id=1, roll=5)
    assert result.busted is False
    assert result.loser_id is None
    assert result.winner_id is None
    assert result.next_active_player == 2


def test_apply_pump_bust_next_active_player_is_none():
    game = _make_game(gauge=90)
    result = apply_pump(game, player_id=1, roll=15)
    assert result.next_active_player is None


# ── first-pump invariant ──────────────────────────────────────────────────────

def test_first_pump_cannot_lose():
    """gauge=0, roll=ROLL_MAX (15) → gauge=15, not busted."""
    game = _make_game(gauge=0)
    result = apply_pump(game, player_id=1, roll=ROLL_MAX)
    assert result.busted is False
    assert result.gauge_after == ROLL_MAX
    assert game.state == "ACTIVE"


def test_first_pump_invariant_constants():
    """Sanity: ROLL_MAX < GAUGE_CEILING so the invariant is structurally sound."""
    assert ROLL_MAX < GAUGE_CEILING


# ── error cases ───────────────────────────────────────────────────────────────

def test_apply_pump_wrong_player_raises():
    game = _make_game(active_player=1)
    with pytest.raises(ValueError, match="turn"):
        apply_pump(game, player_id=2, roll=5)


def test_apply_pump_wrong_state_raises():
    game = _make_game(state="PENDING")
    with pytest.raises(ValueError, match="state"):
        apply_pump(game, player_id=1, roll=5)


# ── serialization ─────────────────────────────────────────────────────────────

def test_pumps_to_json_roundtrip():
    pumps = [
        PumpEntry(player_id=1, roll=7, gauge_before=0, ts=1000.0),
        PumpEntry(player_id=2, roll=12, gauge_before=7, ts=1001.0),
    ]
    raw = pumps_to_json(pumps)
    parsed = json.loads(raw)
    assert len(parsed) == 2
    assert parsed[0] == {"player_id": 1, "roll": 7, "gauge_before": 0, "ts": 1000.0}
    assert parsed[1] == {"player_id": 2, "roll": 12, "gauge_before": 7, "ts": 1001.0}


def test_pumps_to_json_empty():
    assert pumps_to_json([]) == "[]"


class _FakeRow(dict):
    """dict that supports attribute-style access like sqlite3.Row."""
    def __getitem__(self, key):
        return super().__getitem__(key)


def _fake_row(**kwargs) -> _FakeRow:
    defaults = {
        "id": 1,
        "guild_id": 9001,
        "channel_id": 100,
        "challenger_id": 1,
        "target_id": 2,
        "state": "ACTIVE",
        "gauge": 0,
        "active_player": 1,
        "pumps_json": "[]",
        "winner_id": None,
        "loser_id": None,
        "stakes_text": None,
        "message_id": None,
        "result_message_id": None,
        "stakes_honored": None,
        "created_at": 1000.0,
        "last_pump_at": None,
        "resolved_at": None,
    }
    defaults.update(kwargs)
    return _FakeRow(defaults)


def test_game_from_row_basic():
    row = _fake_row(gauge=42, state="ACTIVE")
    game = game_from_row(row)
    assert game.gauge == 42
    assert game.state == "ACTIVE"
    assert game.pumps == []


def test_game_from_row_parses_pumps_json():
    pumps_raw = json.dumps([
        {"player_id": 1, "roll": 7, "gauge_before": 0, "ts": 999.0}
    ])
    row = _fake_row(pumps_json=pumps_raw)
    game = game_from_row(row)
    assert len(game.pumps) == 1
    assert game.pumps[0].roll == 7
    assert game.pumps[0].player_id == 1


def test_game_from_row_null_pumps_json():
    row = _fake_row(pumps_json=None)
    game = game_from_row(row)
    assert game.pumps == []
