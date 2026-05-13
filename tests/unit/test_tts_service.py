"""Unit tests for services.tts_service (Tier 1)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Path-shim so the test runs even without the project conftest having been
# imported (matches the pattern in tests/conftest.py).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Stub out edge_tts before importing the service so the import succeeds even
# when the package isn't installed in the test environment.
if "edge_tts" not in sys.modules:
    edge_tts_stub = MagicMock()
    sys.modules["edge_tts"] = edge_tts_stub

from bot_modules.services.tts_service import (  # noqa: E402
    DEFAULT_VOICE,
    MAX_TEXT_LEN,
    TTSGenerationError,
    TTSService,
)


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "tts_cache"


def test_init_creates_cache_dir(cache_dir):
    assert not cache_dir.exists()
    TTSService(cache_dir=cache_dir)
    assert cache_dir.is_dir()


def test_init_sweeps_stale_mp3s(cache_dir):
    cache_dir.mkdir(parents=True)
    stale = cache_dir / "old.mp3"
    stale.write_bytes(b"stale")
    keep = cache_dir / "notes.txt"
    keep.write_text("keep me")

    TTSService(cache_dir=cache_dir)

    assert not stale.exists()
    assert keep.exists(), "non-mp3 files must be untouched"


@pytest.mark.asyncio
async def test_generate_rejects_empty_text(cache_dir):
    svc = TTSService(cache_dir=cache_dir)
    with pytest.raises(TTSGenerationError):
        await svc.generate("   ")


@pytest.mark.asyncio
async def test_generate_rejects_too_long(cache_dir):
    svc = TTSService(cache_dir=cache_dir)
    with pytest.raises(TTSGenerationError):
        await svc.generate("a" * (MAX_TEXT_LEN + 1))


@pytest.mark.asyncio
async def test_generate_rejects_unknown_voice(cache_dir):
    svc = TTSService(cache_dir=cache_dir)
    with pytest.raises(TTSGenerationError):
        await svc.generate("hi", voice="not-a-real-voice")


@pytest.mark.asyncio
async def test_generate_success(cache_dir, monkeypatch):
    """Mock edge_tts.Communicate so the test stays offline."""
    svc = TTSService(cache_dir=cache_dir)

    fake_communicate = MagicMock()

    async def fake_save(path: str) -> None:
        Path(path).write_bytes(b"\xFF\xFB" + b"\x00" * 256)

    fake_communicate.save = AsyncMock(side_effect=fake_save)

    import bot_modules.services.tts_service as svc_mod

    monkeypatch.setattr(
        svc_mod.edge_tts, "Communicate", MagicMock(return_value=fake_communicate)
    )

    path = await svc.generate("hello world", voice=DEFAULT_VOICE)
    assert path.exists()
    assert path.suffix == ".mp3"
    assert path.parent == cache_dir


@pytest.mark.asyncio
async def test_generate_treats_empty_output_as_failure(cache_dir, monkeypatch):
    svc = TTSService(cache_dir=cache_dir)

    fake_communicate = MagicMock()

    async def fake_save(path: str) -> None:
        Path(path).write_bytes(b"")

    fake_communicate.save = AsyncMock(side_effect=fake_save)

    import bot_modules.services.tts_service as svc_mod

    monkeypatch.setattr(
        svc_mod.edge_tts, "Communicate", MagicMock(return_value=fake_communicate)
    )

    with pytest.raises(TTSGenerationError):
        await svc.generate("hello", voice=DEFAULT_VOICE)


def test_cleanup_is_idempotent(cache_dir):
    svc = TTSService(cache_dir=cache_dir)
    p = cache_dir / "x.mp3"
    p.write_bytes(b"a")
    svc.cleanup(p)
    assert not p.exists()
    # Second call must not raise
    svc.cleanup(p)
