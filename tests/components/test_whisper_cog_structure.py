"""Verify whisper cog loads and exposes expected slash group."""
from __future__ import annotations


def test_whisper_cog_module_imports():
    import cogs.whisper_cog  # noqa: F401


def test_whisper_cog_exposes_setup():
    from cogs.whisper_cog import setup
    assert callable(setup)


def test_whisper_cog_class_exists():
    from cogs.whisper_cog import WhisperCog
    assert WhisperCog is not None
