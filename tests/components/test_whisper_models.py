"""Whisper dataclass shape tests."""
from __future__ import annotations

from bot_modules.services.whisper_models import Whisper, WhisperConfig, WhisperGuess


def test_whisper_config_defaults():
    cfg = WhisperConfig(guild_id=1)
    assert cfg.role_id == 0
    assert cfg.channel_id == 0
    assert cfg.log_channel_id == 0


def test_whisper_dataclass_fields():
    w = Whisper(
        id=1, guild_id=2, sender_id=3, target_id=4,
        message="hi", created_at=1.0, state="pending",
        solved=False, exposed=False, guesses_left=3,
        channel_msg_id=None, dm_msg_id=None,
    )
    assert w.state == "pending"
    assert w.guesses_left == 3
    assert w.solved is False


def test_whisper_guess_dataclass_fields():
    g = WhisperGuess(id=1, whisper_id=2, guessed_id=3, correct=True, created_at=1.0)
    assert g.correct is True
