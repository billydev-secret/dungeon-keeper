# Voice Transcription — Feature Spec

Automatically transcribes Discord voice messages (voice notes) posted in text channels, using a local CPU-only [faster-whisper](https://github.com/SYSTRAN/faster-whisper) model, and replies to the voice message with the transcript.

> **Not the Whisper game.** This feature is unrelated to `docs/whisper_spec.md`, which describes the anonymous text-based "whisper" guessing game. They share nothing but the word "whisper": this spec is about speech-to-text via the Whisper ASR model family.

## Commands

None. The feature is a pure `on_message` listener — all configuration lives in the web dashboard (Config → Voice Transcription).

## Behaviour

### Trigger
A message qualifies for transcription when **all** of the following hold:

- It was sent in a guild by a non-bot user.
- It carries Discord's `IS_VOICE_MESSAGE` flag (bit 13) **and** has at least one attachment — i.e. it is a native Discord voice note, not an ordinary audio file upload.
- The guild has voice transcription **enabled** in its config.
- The channel passes the allowlist: an empty allowlist means every channel; otherwise the channel ID must be listed.

### Transcription
The first attachment is downloaded to a temporary file (suffix from its filename, defaulting to `.ogg`) and transcribed off the event loop via `faster_whisper` with `device="cpu"`, `compute_type="int8"`, `beam_size=1`. A typing indicator shows in the channel while transcription runs. Loaded models are cached in-process, one instance per model name.

### Output
On success with non-empty text, the bot **replies** to the voice message with `📝 {transcript}` (no author mention). Empty transcripts and any failure are silent — errors are logged at warning level, nothing is posted.

### Availability
If `faster-whisper` isn't installed, the cog is skipped entirely at setup (logged warning). The dashboard reports availability and per-model cache status.

### Model cache & read-only home
The systemd unit runs with `ProtectHome=read-only`, so the default HuggingFace cache (`~/.cache/huggingface`) is unwritable. The service sets `HF_HOME` to the repo-local `.cache/huggingface` (the unit's only writable path) **before** importing faster-whisper, which also redirects the separate xet download backend. Models load with `local_files_only=True` — transcription never downloads at runtime; models must be pre-fetched via the dashboard download widget.

## Configuration

Per-guild, dashboard-only (admin permission), backed by the API:

| Setting | Values | Default |
|---|---|---|
| `enabled` | on/off | off (no row = disabled) |
| `model_name` | `tiny.en`, `base.en` | `base.en` |
| `channel_ids` | allowlist of channel IDs | empty = all channels |

Routes (`src/web_server/routes/config.py`):

- `PUT /config/voice-transcription` — upsert the guild config; unknown model names fall back to the default.
- `POST /config/voice-transcription/download` — download a model into the local cache (blocking network fetch run off the loop; no-op if already cached). This is the dashboard's model-download widget.
- The `voice_transcription` section of the config payload reports `enabled`, `model_name`, `channel_ids`, faster-whisper `available`, and per-model `cached` status.

## Stored data

One table, `voice_transcription_config`, one row per guild: `guild_id`, `enabled`, `model_name`, `channel_ids` (comma-separated string). Transcripts themselves are not stored anywhere — the reply message is the only output. Downloaded model weights live on disk under `.cache/huggingface/hub`.

## Non-goals

- No live voice-channel transcription — text-channel voice notes only.
- Only the first attachment of a voice message is transcribed (Discord voice notes carry exactly one).
- English-only models (`*.en`); no language detection or multilingual support.
- No user-facing error messages — failures are log-only.
