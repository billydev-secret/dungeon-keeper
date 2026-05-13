"""Tier 1: whisper service pure validation."""
from __future__ import annotations

import pytest

from bot_modules.services.whisper_models import WhisperConfig
from bot_modules.services.whisper_service import (
    ERROR_BOT_DM_FAILED,
    ERROR_NOT_CONFIGURED,
    ERROR_SELF_TARGET,
    ERROR_SENDER_NEEDS_ROLE,
    ERROR_TARGET_NEEDS_ROLE,
    MAX_MESSAGE_LENGTH,
    SendValidationError,
    validate_send,
)

ROLE = 7001


def _cfg(role=ROLE, ch=8001, log=8002) -> WhisperConfig:
    return WhisperConfig(guild_id=1, role_id=role, channel_id=ch, log_channel_id=log)


def test_validate_send_happy_path():
    # raises nothing
    validate_send(
        cfg=_cfg(),
        sender_role_ids={ROLE},
        target_role_ids={ROLE},
        sender_id=1, target_id=2, message="hello",
    )


def test_validate_send_missing_config():
    with pytest.raises(SendValidationError) as exc:
        validate_send(
            cfg=_cfg(role=0),
            sender_role_ids={ROLE},
            target_role_ids={ROLE},
            sender_id=1, target_id=2, message="hi",
        )
    assert exc.value.message == ERROR_NOT_CONFIGURED


def test_validate_send_sender_lacks_role():
    with pytest.raises(SendValidationError) as exc:
        validate_send(
            cfg=_cfg(),
            sender_role_ids=set(),
            target_role_ids={ROLE},
            sender_id=1, target_id=2, message="hi",
        )
    assert exc.value.message == ERROR_SENDER_NEEDS_ROLE


def test_validate_send_target_lacks_role():
    with pytest.raises(SendValidationError) as exc:
        validate_send(
            cfg=_cfg(),
            sender_role_ids={ROLE},
            target_role_ids=set(),
            sender_id=1, target_id=2, message="hi",
        )
    assert exc.value.message == ERROR_TARGET_NEEDS_ROLE


def test_validate_send_self_target():
    with pytest.raises(SendValidationError) as exc:
        validate_send(
            cfg=_cfg(),
            sender_role_ids={ROLE},
            target_role_ids={ROLE},
            sender_id=1, target_id=1, message="hi",
        )
    assert exc.value.message == ERROR_SELF_TARGET


def test_validate_send_empty_message():
    with pytest.raises(SendValidationError):
        validate_send(
            cfg=_cfg(),
            sender_role_ids={ROLE},
            target_role_ids={ROLE},
            sender_id=1, target_id=2, message="   ",
        )


def test_validate_send_message_too_long():
    with pytest.raises(SendValidationError):
        validate_send(
            cfg=_cfg(),
            sender_role_ids={ROLE},
            target_role_ids={ROLE},
            sender_id=1, target_id=2,
            message="x" * (MAX_MESSAGE_LENGTH + 1),
        )


def test_error_strings_present():
    assert ERROR_BOT_DM_FAILED  # used in cog when DM fails
