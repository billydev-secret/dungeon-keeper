"""Whisper service — pure validation helpers + error strings.

Discord I/O is performed by the cog. This module contains business
rules expressed as pure functions, easily unit-tested.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot_modules.services.whisper_models import STATE_PENDING, Whisper, WhisperConfig

MAX_MESSAGE_LENGTH = 1000


def safe_codefence_content(s: str) -> str:
    """Replace triple-backticks with homoglyphs so user content can't break out of a ``` block."""
    return s.replace("```", "ʼʼʼ")

ERROR_NOT_CONFIGURED = "Whispers aren't set up in this server yet."
ERROR_SENDER_NEEDS_ROLE = (
    "You need the Whisper role to send whispers. Use `/whisper optin` to join."
)
ERROR_TARGET_NEEDS_ROLE = "That member hasn't opted in to receive whispers."
ERROR_SELF_TARGET = "You can't whisper yourself."
ERROR_EMPTY_MESSAGE = "Whisper can't be empty."
ERROR_MESSAGE_TOO_LONG = f"Whisper too long (max {MAX_MESSAGE_LENGTH} chars)."
ERROR_BOT_DM_FAILED = "Couldn't deliver — that user has DMs disabled."

ERROR_GUESS_NOT_TARGET = "Only the recipient can guess."
ERROR_GUESS_SELF = "You can't guess yourself."
ERROR_GUESS_ALREADY_SOLVED = "This whisper has already been solved."
ERROR_GUESS_NO_ATTEMPTS = "No more guesses left."

ERROR_ALREADY_DECIDED = "Already decided."
ERROR_EXPOSE_NOT_TARGET = "Only the recipient can expose this."
ERROR_EXPOSE_NEEDS_SOLVE = "Can only expose a solved whisper."


class SendValidationError(Exception):
    """Raised when send-time validation fails. ``.message`` is user-facing."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def is_configured(cfg: WhisperConfig) -> bool:
    return cfg.role_id != 0 and cfg.channel_id != 0 and cfg.log_channel_id != 0


def validate_send(
    *,
    cfg: WhisperConfig,
    sender_role_ids: set[int],
    target_role_ids: set[int],
    sender_id: int,
    target_id: int,
    message: str,
) -> None:
    """Raise SendValidationError if any precondition is unmet. Otherwise return None."""
    if not is_configured(cfg):
        raise SendValidationError(ERROR_NOT_CONFIGURED)
    if cfg.role_id not in sender_role_ids:
        raise SendValidationError(ERROR_SENDER_NEEDS_ROLE)
    if cfg.role_id not in target_role_ids:
        raise SendValidationError(ERROR_TARGET_NEEDS_ROLE)
    if sender_id == target_id:
        raise SendValidationError(ERROR_SELF_TARGET)
    stripped = message.strip()
    if not stripped:
        raise SendValidationError(ERROR_EMPTY_MESSAGE)
    if len(stripped) > MAX_MESSAGE_LENGTH:
        raise SendValidationError(ERROR_MESSAGE_TOO_LONG)


class GuessValidationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class GuessOutcome:
    correct: bool
    attempts_remaining: int
    exhausted: bool


def evaluate_guess(
    whisper: Whisper, *, guesser_id: int, guessed_id: int
) -> GuessOutcome:
    """Pure-logic guess evaluator. Caller is responsible for persisting the
    resulting state changes (insert_guess, decrement_guesses_left, mark_solved)."""
    if guesser_id != whisper.target_id:
        raise GuessValidationError(ERROR_GUESS_NOT_TARGET)
    if guessed_id == guesser_id:
        raise GuessValidationError(ERROR_GUESS_SELF)
    if whisper.solved:
        raise GuessValidationError(ERROR_GUESS_ALREADY_SOLVED)
    if whisper.guesses_left <= 0:
        raise GuessValidationError(ERROR_GUESS_NO_ATTEMPTS)

    correct = guessed_id == whisper.sender_id
    remaining = whisper.guesses_left - 1
    return GuessOutcome(
        correct=correct,
        attempts_remaining=remaining,
        exhausted=(not correct) and remaining == 0,
    )


class TransitionValidationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _check_target(whisper: Whisper, invoker_id: int, msg: str) -> None:
    if invoker_id != whisper.target_id:
        raise TransitionValidationError(msg)


def validate_share(whisper: Whisper, *, invoker_id: int) -> None:
    _check_target(whisper, invoker_id, ERROR_GUESS_NOT_TARGET)
    if whisper.state != STATE_PENDING:
        raise TransitionValidationError(ERROR_ALREADY_DECIDED)


def validate_hide(whisper: Whisper, *, invoker_id: int) -> None:
    _check_target(whisper, invoker_id, ERROR_GUESS_NOT_TARGET)
    if whisper.state != STATE_PENDING:
        raise TransitionValidationError(ERROR_ALREADY_DECIDED)


def validate_expose(whisper: Whisper, *, invoker_id: int) -> None:
    _check_target(whisper, invoker_id, ERROR_EXPOSE_NOT_TARGET)
    if not whisper.solved:
        raise TransitionValidationError(ERROR_EXPOSE_NEEDS_SOLVE)
