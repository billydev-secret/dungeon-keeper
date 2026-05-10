"""Whisper cog data models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WhisperState = Literal["pending", "shared", "hidden"]
STATE_PENDING: WhisperState = "pending"
STATE_SHARED: WhisperState = "shared"
STATE_HIDDEN: WhisperState = "hidden"


@dataclass
class WhisperConfig:
    guild_id: int
    role_id: int = 0
    channel_id: int = 0
    log_channel_id: int = 0
    launcher_message_id: int = 0


@dataclass
class Whisper:
    id: int
    guild_id: int
    sender_id: int
    target_id: int
    message: str
    created_at: float
    state: WhisperState
    solved: bool
    exposed: bool
    guesses_left: int
    channel_msg_id: int | None
    dm_msg_id: int | None


@dataclass
class WhisperGuess:
    id: int
    whisper_id: int
    guessed_id: int
    correct: bool
    created_at: float
