"""Tier 1: whisper service guess logic (pure)."""
from __future__ import annotations

import pytest

from services.whisper_models import Whisper
from services.whisper_service import (
    ERROR_GUESS_ALREADY_SOLVED,
    ERROR_GUESS_NO_ATTEMPTS,
    ERROR_GUESS_NOT_TARGET,
    ERROR_GUESS_SELF,
    GuessOutcome,
    GuessValidationError,
    evaluate_guess,
)

TARGET = 2001
SENDER = 1001


def _w(*, solved: bool = False, guesses_left: int = 3) -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="x",
        created_at=0.0, state="pending", solved=solved, exposed=False,
        guesses_left=guesses_left, channel_msg_id=None, dm_msg_id=None,
    )


def test_guess_correct_returns_correct_outcome():
    out = evaluate_guess(_w(), guesser_id=TARGET, guessed_id=SENDER)
    assert out == GuessOutcome(correct=True, attempts_remaining=2, exhausted=False)


def test_guess_wrong_returns_decremented():
    out = evaluate_guess(_w(), guesser_id=TARGET, guessed_id=999)
    assert out == GuessOutcome(correct=False, attempts_remaining=2, exhausted=False)


def test_guess_wrong_last_attempt_exhausted():
    out = evaluate_guess(_w(guesses_left=1), guesser_id=TARGET, guessed_id=999)
    assert out.correct is False
    assert out.attempts_remaining == 0
    assert out.exhausted is True


def test_guess_by_non_target_raises():
    with pytest.raises(GuessValidationError) as exc:
        evaluate_guess(_w(), guesser_id=9999, guessed_id=SENDER)
    assert exc.value.message == ERROR_GUESS_NOT_TARGET


def test_guess_self_raises():
    with pytest.raises(GuessValidationError) as exc:
        evaluate_guess(_w(), guesser_id=TARGET, guessed_id=TARGET)
    assert exc.value.message == ERROR_GUESS_SELF


def test_guess_already_solved_raises():
    with pytest.raises(GuessValidationError) as exc:
        evaluate_guess(_w(solved=True), guesser_id=TARGET, guessed_id=SENDER)
    assert exc.value.message == ERROR_GUESS_ALREADY_SOLVED


def test_guess_no_attempts_left_raises():
    with pytest.raises(GuessValidationError) as exc:
        evaluate_guess(_w(guesses_left=0), guesser_id=TARGET, guessed_id=SENDER)
    assert exc.value.message == ERROR_GUESS_NO_ATTEMPTS
