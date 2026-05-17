"""Tier 1: whisper reply cap validator (pure)."""
from __future__ import annotations

import pytest

from bot_modules.services.whisper_models import Whisper
from bot_modules.services.whisper_service import (
    ERROR_REPLY_ALREADY_USED,
    ERROR_REPLY_NOT_PARTICIPANT,
    REPLY_LIMIT_PER_WHISPER,
    TransitionValidationError,
    validate_reply,
)

TARGET = 2001
SENDER = 1001
OUTSIDER = 9999
NOW = 1_700_000_000.0


def _w() -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="x",
        created_at=NOW, state="pending", solved=False, exposed=False,
        guesses_left=3, channel_msg_id=None, dm_msg_id=None,
    )


def test_reply_limit_is_one():
    assert REPLY_LIMIT_PER_WHISPER == 1


def test_first_reply_from_target_ok():
    validate_reply(_w(), invoker_id=TARGET, reply_count=0)


def test_first_reply_from_sender_ok():
    """Sender may also reply (e.g. after target initiates), still capped overall."""
    validate_reply(_w(), invoker_id=SENDER, reply_count=0)


def test_second_reply_blocked():
    with pytest.raises(TransitionValidationError) as exc:
        validate_reply(_w(), invoker_id=TARGET, reply_count=1)
    assert exc.value.message == ERROR_REPLY_ALREADY_USED


def test_outsider_cannot_reply():
    with pytest.raises(TransitionValidationError) as exc:
        validate_reply(_w(), invoker_id=OUTSIDER, reply_count=0)
    assert exc.value.message == ERROR_REPLY_NOT_PARTICIPANT


def test_outsider_rejected_before_cap_check():
    """Permission check fires before the cap check — outsider sees the right error."""
    with pytest.raises(TransitionValidationError) as exc:
        validate_reply(_w(), invoker_id=OUTSIDER, reply_count=5)
    assert exc.value.message == ERROR_REPLY_NOT_PARTICIPANT
