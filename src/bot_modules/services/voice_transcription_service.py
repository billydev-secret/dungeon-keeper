"""Voice transcription service — wraps faster-whisper for local CPU transcription."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("dungeonkeeper.voice_transcription")

# The service runs with ProtectHome=read-only, so the default HuggingFace cache
# (~/.cache/huggingface) is unwritable: loading a non-resident model fails, and
# downloading any model fails on a read-only-fs OSError — including the *separate*
# xet backend cache, which download_root/cache_dir alone does NOT redirect. Point
# the entire HF cache tree at this repo-local dir (the unit's only ReadWritePath)
# BEFORE importing faster-whisper, which pulls in huggingface_hub and freezes its
# cache-path constants. setdefault so an explicit HF_HOME still wins.
os.environ.setdefault(
    "HF_HOME",
    str(Path(__file__).resolve().parents[3] / ".cache" / "huggingface"),
)
# The hub cache lives under HF_HOME/hub; pass this explicitly to the loader too so
# offline loads resolve against it regardless of any later HF_HOME override.
_MODEL_ROOT = str(Path(os.environ["HF_HOME"]) / "hub")

try:
    from faster_whisper import WhisperModel as _WhisperModel, download_model as _fw_download
    _AVAILABLE = True
except ImportError:
    _WhisperModel = None  # type: ignore[assignment, misc]
    _fw_download = None  # type: ignore[assignment]
    _AVAILABLE = False
    log.warning("faster-whisper not installed; voice transcription unavailable")

VALID_MODELS = ("tiny.en", "base.en")
DEFAULT_MODEL = "base.en"

_cache: dict[str, Any] = {}
_lock = threading.Lock()
_download_lock = threading.Lock()


def model_is_cached(model_name: str) -> bool:
    """True if the model is present in the local cache and loadable offline.

    Mirrors exactly what :func:`_get_model` needs — offline resolution against
    the same cache root — so a True here means transcription will actually load.
    """
    if not _AVAILABLE:
        return False
    try:
        _fw_download(model_name, cache_dir=_MODEL_ROOT, local_files_only=True)  # type: ignore[misc]
        return True
    except Exception:
        return False


def download_model_to_cache(model_name: str) -> None:
    """Fetch a model into the local (writable) cache. Blocking — call off the loop."""
    if not _AVAILABLE:
        raise RuntimeError("faster-whisper is not installed on the bot host")
    if model_name not in VALID_MODELS:
        raise ValueError(f"unknown model {model_name!r}")
    with _download_lock:
        if model_is_cached(model_name):
            return
        log.info("Downloading Whisper model %r into %s…", model_name, _MODEL_ROOT)
        _fw_download(model_name, cache_dir=_MODEL_ROOT)  # type: ignore[misc]
        log.info("Whisper model %r downloaded", model_name)


@dataclass
class VoiceTranscriptionConfig:
    guild_id: int
    enabled: bool
    model_name: str
    channel_ids: tuple[int, ...]  # allowlist; empty = all channels


def is_available() -> bool:
    return _AVAILABLE


def _get_model(model_name: str) -> Any:
    with _lock:
        if model_name not in _cache:
            log.info("Loading Whisper model %r (first use)…", model_name)
            _cache[model_name] = _WhisperModel(  # type: ignore[operator]
                model_name,
                device="cpu",
                compute_type="int8",
                download_root=_MODEL_ROOT,
                local_files_only=True,
            )
        return _cache[model_name]


def transcribe_file(path: Path, model_name: str = DEFAULT_MODEL) -> str:
    """Transcribe an audio file; returns the full transcript as a single string."""
    model = _get_model(model_name)
    segments, _ = model.transcribe(str(path), beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


# ── DB helpers ────────────────────────────────────────────────────────────────
#
# These take an open sqlite3 connection so web routes (which already hold one)
# and the cog listener (via open_db in a worker thread) share the same code.


def _parse_channel_ids(raw: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in (raw or "").split(",") if p.strip())


def get_config(conn: Any, guild_id: int) -> VoiceTranscriptionConfig | None:
    row = conn.execute(
        "SELECT enabled, model_name, channel_ids "
        "FROM voice_transcription_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is None:
        return None
    return VoiceTranscriptionConfig(
        guild_id=guild_id,
        enabled=bool(row["enabled"]),
        model_name=row["model_name"],
        channel_ids=_parse_channel_ids(row["channel_ids"]),
    )


def set_config(
    conn: Any,
    guild_id: int,
    *,
    enabled: bool,
    model_name: str,
    channel_ids: tuple[int, ...] = (),
) -> None:
    if model_name not in VALID_MODELS:
        model_name = DEFAULT_MODEL
    csv = ",".join(str(int(c)) for c in channel_ids)
    conn.execute(
        """
        INSERT INTO voice_transcription_config (guild_id, enabled, model_name, channel_ids)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (guild_id) DO UPDATE SET
            enabled = excluded.enabled,
            model_name = excluded.model_name,
            channel_ids = excluded.channel_ids
        """,
        (guild_id, int(enabled), model_name, csv),
    )
