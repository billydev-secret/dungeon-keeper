"""Unit tests for hot_potato_group/game.py (pure logic, no Discord)."""
from __future__ import annotations

from bot_modules.cogs.hot_potato_group.game import (
    HotPotatoGroupGame,
    bravest,
    cumulative_hold_times,
    game_from_row,
    next_holder_clockwise,
    shake_emoji,
)


# ── next_holder_clockwise ──────────────────────────────────────────────────────

def test_next_holder_advances():
    assert next_holder_clockwise([1, 2, 3], 1) == 2
    assert next_holder_clockwise([1, 2, 3], 2) == 3


def test_next_holder_wraps_around():
    assert next_holder_clockwise([1, 2, 3], 3) == 1


def test_next_holder_current_not_in_list_returns_first():
    assert next_holder_clockwise([4, 5, 6], 99) == 4


def test_next_holder_single_player_returns_self():
    assert next_holder_clockwise([7], 7) == 7


# ── cumulative_hold_times ──────────────────────────────────────────────────────

def test_cumulative_hold_times_sums_per_player():
    log = [
        {"holder_id": 1, "received_at": 0.0, "passed_at": 3.0},
        {"holder_id": 2, "received_at": 3.0, "passed_at": 5.0},
        {"holder_id": 1, "received_at": 5.0, "passed_at": 6.0},
    ]
    holds = cumulative_hold_times(log, end_ts=6.0)
    assert holds[1] == 4.0  # 3 + 1
    assert holds[2] == 2.0


def test_cumulative_hold_times_open_entry_uses_end_ts():
    log = [
        {"holder_id": 1, "received_at": 0.0, "passed_at": 4.0},
        {"holder_id": 2, "received_at": 4.0, "passed_at": None},
    ]
    holds = cumulative_hold_times(log, end_ts=10.0)
    assert holds[2] == 6.0


def test_cumulative_hold_times_empty():
    assert cumulative_hold_times([], end_ts=5.0) == {}


# ── bravest ────────────────────────────────────────────────────────────────────

def test_bravest_picks_max_holder():
    assert bravest({1: 2.0, 2: 9.0, 3: 4.0}) == 2


def test_bravest_empty_is_none():
    assert bravest({}) is None


# ── shake_emoji ────────────────────────────────────────────────────────────────

def test_shake_emoji_escalates():
    assert shake_emoji(0.0, 10.0) == "🥔💣"
    assert shake_emoji(7.5, 10.0) == "🥔💣💥"      # ≥ 0.70
    assert shake_emoji(9.5, 10.0) == "🥔💣💥💥"     # ≥ 0.90


def test_shake_emoji_zero_fuse_is_max():
    assert shake_emoji(1.0, 0.0) == "🥔💣💥💥"


# ── dataclass + game_from_row ──────────────────────────────────────────────────

def test_challenger_id_aliases_host_id():
    game = HotPotatoGroupGame(
        id=1, guild_id=1, channel_id=1, host_id=42, state="LOBBY", roster=[42]
    )
    assert game.challenger_id == 42


def _row(**kwargs):
    defaults = dict(
        id=1,
        guild_id=100,
        channel_id=200,
        host_id=10,
        state="LOBBY",
        round=0,
        roster="[10]",
        alive="[]",
        elimination_order="[]",
        holder_id=None,
        winner_id=None,
        loser_id=None,
        stakes_text=None,
        message_id=None,
        result_message_id=None,
        fuse_seconds=None,
        phase_started_at=None,
        pass_log="[]",
        last_action_at=None,
        resolved_at=None,
        created_at=1000.0,
    )
    defaults.update(kwargs)
    return defaults


def test_game_from_row_parses_json_columns():
    game = game_from_row(
        _row(roster="[10, 20, 30]", alive="[20, 30]", elimination_order="[10]")
    )
    assert game.roster == [10, 20, 30]
    assert game.alive == [20, 30]
    assert game.elimination_order == [10]
    assert game.host_id == 10
    assert game.challenger_id == 10


def test_game_from_row_null_json_defaults_to_empty():
    game = game_from_row(_row(roster=None, alive=None, elimination_order=None, pass_log=None))
    assert game.roster == []
    assert game.alive == []
    assert game.elimination_order == []
    assert game.pass_log == []
