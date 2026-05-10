"""Tier 1: state transition validators (pure)."""
from __future__ import annotations

import pytest

from services.whisper_models import Whisper, WhisperState
from services.whisper_service import (
    ERROR_ALREADY_DECIDED,
    ERROR_EXPOSE_NEEDS_SOLVE,
    ERROR_EXPOSE_NOT_TARGET,
    ERROR_GUESS_NOT_TARGET,
    TransitionValidationError,
    validate_expose,
    validate_hide,
    validate_share,
)

TARGET = 2001
SENDER = 1001


def _w(*, state: WhisperState = "pending", solved: bool = False) -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="x",
        created_at=0.0, state=state, solved=solved, exposed=False,
        guesses_left=3, channel_msg_id=None, dm_msg_id=None,
    )


def test_validate_share_pending_ok():
    validate_share(_w(state="pending"), invoker_id=TARGET)


def test_validate_share_already_shared_raises():
    with pytest.raises(TransitionValidationError) as exc:
        validate_share(_w(state="shared"), invoker_id=TARGET)
    assert exc.value.message == ERROR_ALREADY_DECIDED


def test_validate_share_hidden_raises():
    with pytest.raises(TransitionValidationError):
        validate_share(_w(state="hidden"), invoker_id=TARGET)


def test_validate_share_non_target_raises():
    with pytest.raises(TransitionValidationError) as exc:
        validate_share(_w(state="pending"), invoker_id=9999)
    assert exc.value.message == ERROR_GUESS_NOT_TARGET


def test_validate_hide_pending_ok():
    validate_hide(_w(state="pending"), invoker_id=TARGET)


def test_validate_hide_non_pending_raises():
    with pytest.raises(TransitionValidationError):
        validate_hide(_w(state="shared"), invoker_id=TARGET)


def test_validate_expose_requires_solved():
    with pytest.raises(TransitionValidationError) as exc:
        validate_expose(_w(solved=False), invoker_id=TARGET)
    assert exc.value.message == ERROR_EXPOSE_NEEDS_SOLVE


def test_validate_expose_solved_target_ok():
    validate_expose(_w(solved=True), invoker_id=TARGET)


def test_validate_expose_non_target_raises():
    with pytest.raises(TransitionValidationError) as exc:
        validate_expose(_w(solved=True), invoker_id=9999)
    assert exc.value.message == ERROR_EXPOSE_NOT_TARGET
