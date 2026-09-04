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
    #: Remove the voice message once its transcript has been posted. Off by
    #: default: the delete is irreversible and the transcript cannot be checked
    #: against the audio afterwards.
    delete_after_transcribe: bool = False


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


#: Discord message content caps at 2000 characters; this is the per-message
#: budget with headroom, and it counts the whole message — the speaker prefix
#: included, which is why both fitters take that prefix rather than trusting
#: the slack to absorb it.
MAX_TRANSCRIPT_CHARS = 1900
_TRUNCATED_NOTE = "\n\n*(transcript truncated — the recording was longer than one message)*"


def _cut(text: str, limit: int) -> tuple[str, str]:
    """Split *text* at *limit*, preferring a word boundary in the last fifth.

    Returns ``(head, rest)`` — ``head`` never exceeds *limit*, and *rest* opens
    on a word rather than on the space the cut landed in. A single word longer
    than the whole budget has no boundary to find and is cut mid-word, which is
    the only way it can be shown at all.
    """
    head = text[:limit]
    space = head.rfind(" ")
    if space > limit * 0.8:
        head = head[:space]
    return head.rstrip(), text[len(head):].lstrip()


def fit_transcript(
    text: str, limit: int = MAX_TRANSCRIPT_CHARS, prefix: str = ""
) -> str:
    """Trim a transcript to one Discord message, saying so when it trims.

    The truncation note is paid for out of the budget rather than added on top,
    so the returned message — prefix and all — is never longer than *limit*.

    Used by the automatic listener, which posts one message per voice note by
    design: an auto-post nobody asked for shouldn't be able to fill a channel.
    The on-demand context menu splits instead; see :func:`split_transcript`.
    """
    budget = max(1, limit - len(prefix))
    if len(text) <= budget:
        return prefix + text
    head, _ = _cut(text, max(1, budget - len(_TRUNCATED_NOTE)))
    return prefix + head + _TRUNCATED_NOTE


def was_truncated(message: str) -> bool:
    """Whether a message built by :func:`fit_transcript` had to be cut.

    The listener asks because the clip is the only copy of what the cut
    removed: deleting the audio behind a truncated transcript would destroy
    the tail of what someone said with nothing left to recover it from.
    """
    return message.endswith(_TRUNCATED_NOTE)


#: How many messages an *uploaded* audio file may occupy. A real voice note is
#: uncapped — someone who pressed the button wants the whole note — but the
#: context menu also accepts any ``audio/*`` upload, and an hour-long podcast
#: would otherwise post hundreds of messages into a channel and outlive the
#: 15-minute interaction token part-way through. Ten parts is around 25 minutes
#: of speech: past any real voice note, short of a flood.
MAX_UPLOAD_PARTS = 10


def split_transcript(
    text: str,
    limit: int = MAX_TRANSCRIPT_CHARS,
    prefix: str = "",
    max_parts: int | None = None,
) -> list[str]:
    """Spread a transcript over as many messages as it takes, losing nothing.

    By default there is no cap on the number of parts: someone who explicitly
    asked for a transcript wants the whole thing, and a cut long note reads
    like a transcription failure. Pass *max_parts* to bound it — the last
    allowed part is fitted rather than cut bare, so a capped transcript ends
    with the same truncation note the listener uses and never simply stops.

    Only the first part carries *prefix* — repeating ``📝 **Name:**`` on each
    one would read as several separate notes rather than one continued.

    Empty (or whitespace-only) text yields no messages at all, so a caller
    never posts a bare prefix with nothing after it.
    """
    limit = max(1, limit)
    parts: list[str] = []
    rest = text.strip()
    while rest:
        head, budget = (prefix, limit - len(prefix)) if not parts else ("", limit)
        budget = max(1, budget)
        if max_parts is not None and len(parts) + 1 >= max_parts:
            parts.append(head + fit_transcript(rest, budget))
            break
        if len(rest) <= budget:
            parts.append(head + rest)
            break
        chunk, rest = _cut(rest, budget)
        parts.append(head + chunk)
    return parts


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
        "SELECT enabled, model_name, channel_ids, delete_after_transcribe "
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
        delete_after_transcribe=bool(row["delete_after_transcribe"]),
    )


def set_config(
    conn: Any,
    guild_id: int,
    *,
    enabled: bool,
    model_name: str,
    channel_ids: tuple[int, ...] = (),
    delete_after_transcribe: bool = False,
) -> None:
    if model_name not in VALID_MODELS:
        model_name = DEFAULT_MODEL
    csv = ",".join(str(int(c)) for c in channel_ids)
    conn.execute(
        """
        INSERT INTO voice_transcription_config
            (guild_id, enabled, model_name, channel_ids, delete_after_transcribe)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (guild_id) DO UPDATE SET
            enabled = excluded.enabled,
            model_name = excluded.model_name,
            channel_ids = excluded.channel_ids,
            delete_after_transcribe = excluded.delete_after_transcribe
        """,
        (guild_id, int(enabled), model_name, csv, int(delete_after_transcribe)),
    )
