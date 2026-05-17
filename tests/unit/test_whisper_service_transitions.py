"""Tier 1: state transition validators (pure)."""
from __future__ import annotations

import pytest

from bot_modules.services.whisper_models import Whisper, WhisperState
from bot_modules.services.whisper_service import (
    ERROR_ALREADY_DECIDED,
    ERROR_ALREADY_DELETED,
    ERROR_DELETE_NOT_TARGET,
    ERROR_EXPOSE_NEEDS_SOLVE,
    ERROR_EXPOSE_NOT_TARGET,
    ERROR_GUESS_NOT_TARGET,
    TransitionValidationError,
    validate_delete,
    validate_expose,
    validate_hide,
    validate_share,
)

TARGET = 2001
SENDER = 1001
NOW = 1_700_000_000.0


def _w(
    *,
    state: WhisperState = "pending",
    solved: bool = False,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="x",
        created_at=NOW, state=state, solved=solved, exposed=False,
        guesses_left=3, channel_msg_id=None, dm_msg_id=None,
        deleted_at=deleted_at,
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


def test_validate_delete_target_ok():
    validate_delete(_w(), invoker_id=TARGET)


def test_validate_delete_already_deleted_raises():
    with pytest.raises(TransitionValidationError) as exc:
        validate_delete(_w(deleted_at=NOW), invoker_id=TARGET)
    assert exc.value.message == ERROR_ALREADY_DELETED


def test_validate_delete_non_target_raises():
    with pytest.raises(TransitionValidationError) as exc:
        validate_delete(_w(), invoker_id=SENDER)
    assert exc.value.message == ERROR_DELETE_NOT_TARGET


def test_validate_delete_allowed_on_shared_whisper():
    """Delete works regardless of share state — target controls their inbox."""
    validate_delete(_w(state="shared"), invoker_id=TARGET)
