"""Unit tests for quickdraw/game.py — pure logic, no Discord."""
from __future__ import annotations

import time

from bot_modules.cogs.quickdraw.game import QuickdrawGame, game_from_row


def _make_game(
    *,
    state: str = "ACTIVE",
    qd_state: str = "WAITING",
    challenger_id: int = 1,
    target_id: int = 2,
    winner_id: int | None = None,
    loser_id: int | None = None,
    fired_at: float | None = None,
) -> QuickdrawGame:
    return QuickdrawGame(
        id=1,
        guild_id=9001,
        channel_id=100,
        challenger_id=challenger_id,
        target_id=target_id,
        state=state,
        qd_state=qd_state,
        winner_id=winner_id,
        loser_id=loser_id,
        fired_at=fired_at,
    )


# ── dataclass defaults ────────────────────────────────────────────────────────

def test_default_qd_state_is_waiting():
    game = _make_game()
    assert game.qd_state == "WAITING"


def test_default_fired_at_is_none():
    game = _make_game()
    assert game.fired_at is None


def test_winner_loser_default_none():
    game = _make_game()
    assert game.winner_id is None
    assert game.loser_id is None


# ── game_from_row ─────────────────────────────────────────────────────────────

class _FakeRow(dict):
    """dict subclass that supports row["key"] access."""


def _make_row(**kwargs) -> _FakeRow:
    defaults = {
        "id": 7,
        "guild_id": 9001,
        "channel_id": 100,
        "challenger_id": 1,
        "target_id": 2,
        "state": "ACTIVE",
        "qd_state": "DRAW",
        "winner_id": None,
        "loser_id": None,
        "stakes_text": None,
        "message_id": None,
        "result_message_id": None,
        "draw_delay": 4.5,
        "fired_at": 1000.0,
        "last_action_at": None,
        "resolved_at": None,
        "created_at": 900.0,
    }
    defaults.update(kwargs)
    return _FakeRow(defaults)


def test_game_from_row_basic():
    row = _make_row()
    game = game_from_row(row)
    assert game.id == 7
    assert game.guild_id == 9001
    assert game.qd_state == "DRAW"
    assert game.fired_at == 1000.0
    assert game.draw_delay == 4.5


def test_game_from_row_null_created_at_defaults_to_now():
    row = _make_row(created_at=None)
    before = time.time()
    game = game_from_row(row)
    after = time.time()
    assert before <= game.created_at <= after


def test_game_from_row_preserves_winner_loser():
    row = _make_row(winner_id=2, loser_id=1, qd_state="COMPLETE")
    game = game_from_row(row)
    assert game.winner_id == 2
    assert game.loser_id == 1
    assert game.qd_state == "COMPLETE"


def test_game_from_row_null_fired_at():
    row = _make_row(fired_at=None, qd_state="WAITING")
    game = game_from_row(row)
    assert game.fired_at is None
    assert game.qd_state == "WAITING"


# ── false-start vs clean-draw detection ──────────────────────────────────────

def test_false_start_has_no_fired_at():
    game = _make_game(qd_state="COMPLETE", winner_id=2, loser_id=1, fired_at=None)
    assert game.fired_at is None


def test_clean_draw_has_fired_at():
    game = _make_game(qd_state="COMPLETE", winner_id=1, loser_id=2, fired_at=1234.5)
    assert game.fired_at == 1234.5
