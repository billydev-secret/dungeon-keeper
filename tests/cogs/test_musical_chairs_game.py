"""Unit tests for musical_chairs/game.py (pure logic, no Discord)."""
from __future__ import annotations

from bot_modules.cogs.musical_chairs.game import (
    MusicalChairsGame,
    chairs_for,
    game_from_row,
    is_false_start,
    resolve_round,
)


# ── chairs_for ─────────────────────────────────────────────────────────────────

def test_chairs_for_is_n_minus_one():
    assert chairs_for(5) == 4
    assert chairs_for(2) == 1


def test_chairs_for_never_negative():
    assert chairs_for(1) == 0
    assert chairs_for(0) == 0


# ── is_false_start ─────────────────────────────────────────────────────────────

def test_is_false_start_only_during_music():
    assert is_false_start("MUSIC") is True
    assert is_false_start("SCRAMBLE") is False
    assert is_false_start(None) is False


# ── resolve_round ──────────────────────────────────────────────────────────────

def test_resolve_round_one_unseated_eliminated():
    survivors, eliminated = resolve_round([1, 2, 3], seated=[2, 3], chairs=2)
    assert survivors == [2, 3]
    assert eliminated == [1]


def test_resolve_round_extra_sitter_loses_seat():
    # all three sat, only 2 chairs → the slowest (last in press order) is out
    survivors, eliminated = resolve_round([1, 2, 3], seated=[3, 2, 1], chairs=2)
    assert set(survivors) == {3, 2}
    assert eliminated == [1]


def test_resolve_round_survivor_order_follows_alive():
    survivors, _ = resolve_round([1, 2, 3], seated=[3, 2], chairs=2)
    assert survivors == [2, 3]  # alive order, not press order


def test_resolve_round_no_show_multi_elimination():
    # only one of three pressed; 2 chairs → two players out
    survivors, eliminated = resolve_round([1, 2, 3], seated=[1], chairs=2)
    assert survivors == [1]
    assert eliminated == [2, 3]


def test_resolve_round_final_two_one_chair():
    survivors, eliminated = resolve_round([1, 2], seated=[2], chairs=1)
    assert survivors == [2]
    assert eliminated == [1]


def test_resolve_round_ignores_dead_in_seated():
    survivors, eliminated = resolve_round([1, 2], seated=[99, 2], chairs=1)
    assert survivors == [2]
    assert eliminated == [1]


# ── game_from_row / dataclass ──────────────────────────────────────────────────

def test_challenger_id_aliases_host_id():
    g = MusicalChairsGame(id=1, guild_id=1, channel_id=1, host_id=77, state="LOBBY")
    assert g.challenger_id == 77


def _row(**kwargs):
    defaults = dict(
        id=1, guild_id=100, channel_id=200, host_id=10, state="LOBBY",
        phase=None, round=0, chairs=None,
        roster="[10]", alive="[]", elimination_order="[]", seated="[]",
        winner_id=None, loser_id=None, stakes_text=None,
        message_id=None, result_message_id=None,
        phase_started_at=None, phase_duration=None,
        last_action_at=None, resolved_at=None, created_at=1000.0,
    )
    defaults.update(kwargs)
    return defaults


def test_game_from_row_parses_json():
    g = game_from_row(_row(roster="[1,2,3]", alive="[2,3]", elimination_order="[1]", seated="[2]"))
    assert g.roster == [1, 2, 3]
    assert g.alive == [2, 3]
    assert g.elimination_order == [1]
    assert g.seated == [2]


def test_game_from_row_null_json_empty():
    g = game_from_row(_row(roster=None, alive=None, elimination_order=None, seated=None))
    assert g.roster == [] and g.alive == [] and g.elimination_order == [] and g.seated == []
