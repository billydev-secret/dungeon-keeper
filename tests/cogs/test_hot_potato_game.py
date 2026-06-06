"""Unit tests for hot_potato/game.py."""
from __future__ import annotations

import time

from bot_modules.cogs.hot_potato.game import (
    HotPotatoGame,
    compute_style_points,
    game_from_row,
)


# ── compute_style_points ──────────────────────────────────────────────────────

def test_no_danger_zone_time_returns_no_points():
    started = time.time()
    timer = 20.0
    pass_log = [
        {"holder_id": 1, "received_at": started, "passed_at": started + 5.0},
        {"holder_id": 2, "received_at": started + 5.0, "passed_at": started + 10.0},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=2, winner_id=1)
    assert pts == {}


def test_loser_earns_points_for_holding_in_danger_zone():
    started = 0.0
    timer = 10.0
    # danger zone starts at 7.0; loser held from 7.5 to 10.0 = 2.5s → 25 pts
    pass_log = [
        {"holder_id": 1, "received_at": 0.0, "passed_at": 7.5},
        {"holder_id": 2, "received_at": 7.5, "passed_at": 10.0},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=2, winner_id=1)
    assert pts.get(2, 0) == 25


def test_winner_earns_points_for_holding_in_danger_zone():
    started = 0.0
    timer = 10.0
    # danger zone starts at 7.0; winner held from 7.0 to 9.0 = 2s → 20 pts, then passed
    # loser received at 9.0, held until explosion at 10.0 = 1s → 10 pts
    pass_log = [
        {"holder_id": 1, "received_at": 0.0, "passed_at": 7.0},
        {"holder_id": 2, "received_at": 7.0, "passed_at": 9.0},
        {"holder_id": 1, "received_at": 9.0, "passed_at": 10.0},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=1, winner_id=2)
    assert pts.get(2, 0) == 20
    assert pts.get(1, 0) == 10


def test_open_entry_uses_explosion_as_end():
    started = 0.0
    timer = 10.0
    # loser holds from 8.0 until explosion (10.0), passed_at is None
    pass_log = [
        {"holder_id": 1, "received_at": 0.0, "passed_at": 8.0},
        {"holder_id": 2, "received_at": 8.0, "passed_at": None},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=2, winner_id=1)
    assert pts.get(2, 0) == 20  # 2s × 10 = 20


def test_partial_overlap_is_correct():
    started = 0.0
    timer = 10.0
    # danger zone 7.0–10.0; holder was from 6.0 to 8.0 → overlap is 7.0–8.0 = 1.0s → 10 pts
    pass_log = [
        {"holder_id": 1, "received_at": 6.0, "passed_at": 8.0},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=2, winner_id=1)
    assert pts.get(1, 0) == 10


def test_no_points_for_zero_overlap():
    started = 0.0
    timer = 10.0
    pass_log = [
        {"holder_id": 1, "received_at": 0.0, "passed_at": 6.9},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=2, winner_id=1)
    assert pts.get(1, 0) == 0


def test_multiple_spells_accumulate():
    started = 0.0
    timer = 10.0
    # Same holder holds twice in danger zone: 7.0–7.5 = 0.5s and 8.0–9.0 = 1.0s → 15 pts
    pass_log = [
        {"holder_id": 1, "received_at": 7.0, "passed_at": 7.5},
        {"holder_id": 2, "received_at": 7.5, "passed_at": 8.0},
        {"holder_id": 1, "received_at": 8.0, "passed_at": 9.0},
    ]
    pts = compute_style_points(pass_log, started, timer, loser_id=2, winner_id=1)
    assert pts.get(1, 0) == 15


# ── game_from_row ─────────────────────────────────────────────────────────────

def _row(**kwargs):
    defaults = dict(
        id=1,
        guild_id=100,
        channel_id=200,
        challenger_id=10,
        target_id=20,
        state="PENDING",
        holder_id=None,
        winner_id=None,
        loser_id=None,
        stakes_text=None,
        message_id=None,
        result_message_id=None,
        timer_seconds=None,
        started_at=None,
        pass_log="[]",
        last_action_at=None,
        resolved_at=None,
        created_at=1000.0,
    )
    defaults.update(kwargs)
    return defaults


def test_game_from_row_defaults():
    game = game_from_row(_row())
    assert game.id == 1
    assert game.state == "PENDING"
    assert game.pass_log == []
    assert game.holder_id is None


def test_game_from_row_parses_pass_log():
    log = '[{"holder_id": 1, "received_at": 1.0, "passed_at": null}]'
    game = game_from_row(_row(pass_log=log, holder_id=1))
    assert len(game.pass_log) == 1
    assert game.pass_log[0]["holder_id"] == 1
    assert game.pass_log[0]["passed_at"] is None


def test_game_from_row_null_pass_log_is_empty_list():
    game = game_from_row(_row(pass_log=None))
    assert game.pass_log == []


def test_game_from_row_preserves_timer_fields():
    game = game_from_row(_row(timer_seconds=30.5, started_at=1000.0))
    assert game.timer_seconds == 30.5
    assert game.started_at == 1000.0


# ── HotPotatoGame dataclass ───────────────────────────────────────────────────

def test_hot_potato_game_default_pass_log():
    game = HotPotatoGame(
        id=1, guild_id=1, channel_id=1,
        challenger_id=10, target_id=20, state="PENDING",
    )
    assert game.pass_log == []
    assert game.holder_id is None
