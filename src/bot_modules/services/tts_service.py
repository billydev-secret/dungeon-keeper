"""TTS audio generation via edge-tts.

Generates MP3 files into ``lavalink/tts_cache/`` so Lavalink's local file
source can stream them through the existing wavelink player. The cache dir
sits inside Lavalink's working directory (set in ``LavalinkManager``) so
absolute paths resolve cleanly on both sides.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import edge_tts

log = logging.getLogger("dungeonkeeper.tts")

_BOT_ROOT = Path(__file__).resolve().parents[1]
_CACHE_DIR = _BOT_ROOT / "lavalink" / "tts_cache"

DEFAULT_VOICE = "en-US-AriaNeural"
MAX_TEXT_LEN = 500


@dataclass(frozen=True)
class VoiceChoice:
    value: str
    label: str


VOICE_CHOICES: tuple[VoiceChoice, ...] = (
    VoiceChoice("en-US-AriaNeural", "Aria (US, female)"),
    VoiceChoice("en-US-GuyNeural", "Guy (US, male)"),
    VoiceChoice("en-US-JennyNeural", "Jenny (US, female)"),
    VoiceChoice("en-GB-SoniaNeural", "Sonia (UK, female)"),
    VoiceChoice("en-GB-RyanNeural", "Ryan (UK, male)"),
    VoiceChoice("en-AU-NatashaNeural", "Natasha (AU, female)"),
    VoiceChoice("en-IE-EmilyNeural", "Emily (IE, female)"),
    VoiceChoice("en-CA-ClaraNeural", "Clara (CA, female)"),
)

_VALID_VOICES = frozenset(v.value for v in VOICE_CHOICES)


class TTSGenerationError(RuntimeError):
    pass


class TTSService:
    """Generates TTS MP3s and cleans up the cache directory.

    The constructor wipes any leftover MP3s from a previous run. Files are
    deleted individually after each playback finishes (see TTSPlaybackService).
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale()

    def _sweep_stale(self) -> None:
        for f in self.cache_dir.glob("*.mp3"):
            try:
                f.unlink()
            except OSError as exc:
                log.warning("could not remove stale tts file %s: %s", f, exc)

    @staticmethod
    def is_valid_voice(voice: str) -> bool:
        return voice in _VALID_VOICES

    async def generate(self, text: str, voice: str = DEFAULT_VOICE) -> Path:
        text = text.strip()
        if not text:
            raise TTSGenerationError("Text is empty.")
        if len(text) > MAX_TEXT_LEN:
            raise TTSGenerationError(
                f"Text exceeds {MAX_TEXT_LEN} characters."
            )
        if not self.is_valid_voice(voice):
            raise TTSGenerationError(f"Unknown voice: {voice!r}.")

        path = self.cache_dir / f"{uuid.uuid4().hex}.mp3"
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(path))
        except Exception as exc:
            log.exception("edge-tts generation failed")
            with _suppress_oserror():
                path.unlink(missing_ok=True)
            raise TTSGenerationError(str(exc)) from exc

        if not path.exists() or path.stat().st_size == 0:
            with _suppress_oserror():
                path.unlink(missing_ok=True)
            raise TTSGenerationError("edge-tts produced an empty file.")

        log.info("tts generated %s (%d bytes, voice=%s)", path.name, path.stat().st_size, voice)
        return path

    @staticmethod
    def cleanup(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove tts file %s: %s", path, exc)


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)
