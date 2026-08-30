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
    cooldown_seconds: int = 30
    hourly_cap_per_target: int = 5
    # How many guesses the recipient gets to unmask the sender. The schema
    # seeds whispers.guesses_left from this; it is fixed for the life of a
    # whisper, so changing it only affects ones sent afterwards.
    guesses_per_whisper: int = 3


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
    deleted_at: float | None = None


@dataclass
class WhisperGuess:
    id: int
    whisper_id: int
    guessed_id: int
    correct: bool
    created_at: float


@dataclass
class WhisperReply:
    id: int
    whisper_id: int
    from_user_id: int
    to_user_id: int
    content: str
    created_at: float


@dataclass
class WhisperReplyReport:
    id: int
    reply_id: int
    reporter_id: int
    reason: str
    created_at: float
