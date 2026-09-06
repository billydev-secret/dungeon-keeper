"""Whisper cog data models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Two states, and the dashboard's audit filter enumerates exactly these
# (tests/components/test_whisper_web_routes.py). "hidden" was a third once;
# migration 025 folded it into the soft-delete flag and nothing writes it.
WhisperState = Literal["pending", "shared"]
STATE_PENDING: WhisperState = "pending"
STATE_SHARED: WhisperState = "shared"


@dataclass
class WhisperConfig:
    guild_id: int
    role_id: int = 0
    channel_id: int = 0
    log_channel_id: int = 0
    launcher_message_id: int = 0
    # Where the launcher actually is. Stored beside the message id (as the
    # Guess prompt does) so ``core.sticky`` deletes the old launcher through
    # its real channel after an admin repoints ``channel_id``. Zero for a
    # launcher posted before this key existed — the cog falls back to
    # ``channel_id`` for those.
    launcher_channel_id: int = 0
    # DM the sender after each guess on their whisper (wrong / caught /
    # target out of guesses). Ships OFF: whispers already in flight in prod
    # must not start DMing their senders until an admin flips it.
    sender_feedback: bool = False
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
    guesses_left: int
    channel_msg_id: int | None
    dm_msg_id: int | None
    deleted_at: float | None = None
    # The ``whispers.exposed`` column survives (dropping it needs a table
    # rebuild) but nothing writes or reads it any more: the Expose button was
    # detached from every view in 1396fb5e and the 31 prod rows that carry it
    # are all older than the 30-day age lock. Kept on the dataclass so the row
    # mapper stays a plain column-for-column copy.
    exposed: bool = False


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
