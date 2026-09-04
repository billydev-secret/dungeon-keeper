# Voice Transcription — Feature Spec

Automatically transcribes Discord voice messages (voice notes) posted in text channels, using a local CPU-only [faster-whisper](https://github.com/SYSTRAN/faster-whisper) model, and replies to the voice message with the transcript.

> **Not the Whisper game.** This feature is unrelated to `docs/whisper_spec.md`, which describes the anonymous text-based "whisper" guessing game. They share nothing but the word "whisper": this spec is about speech-to-text via the Whisper ASR model family.

## Commands

One message **context menu**: **Transcribe Voice Note** (long-press a message on mobile, or right-click → Apps on desktop). Everything else is a pure `on_message` listener, and all configuration lives in the web dashboard (Config → Voice Transcription).

The menu is registered with `allowed_installs(guilds=True, users=True)` and `allowed_contexts(guilds=True, dms=True, private_channels=True)` — i.e. it is a **user-installable** command. That is what lets it run in a personal DM: a bot cannot read or post in a DM it is not part of, but a user-installed command travels with the person who installed it and answers through the interaction. It requires **User Install** to be enabled for the app in the Discord Developer Portal; without that, the command simply never appears outside guilds.

## Behavior

### Trigger
A message qualifies for transcription when **all** of the following hold:

- It was sent in a guild by a non-bot user.
- It carries Discord's `IS_VOICE_MESSAGE` flag (bit 13) **and** has at least one attachment — i.e. it is a native Discord voice note, not an ordinary audio file upload.
- The guild has voice transcription **enabled** in its config.
- The channel passes the allowlist: an empty allowlist means every channel; otherwise the channel ID must be listed.

### Transcription
The first attachment is downloaded to a temporary file (suffix from its filename, defaulting to `.ogg`) and transcribed off the event loop via `faster_whisper` with `device="cpu"`, `compute_type="int8"`, `beam_size=1`. A typing indicator shows in the channel while transcription runs. Loaded models are cached in-process, one instance per model name.

### Output
On success with non-empty text, the bot posts a **standalone message** — not a reply — reading `📝 **{speaker}:** {transcript}`, with the speaker's display name markdown-escaped. It is standalone because `delete_after_transcribe` may remove the voice message, and a reply to a deleted message renders as a dangling stub. Empty transcripts and any failure are silent on the listener path — errors are logged at warning level, nothing is posted.

### On-demand transcription (context menu)
The menu path is **not** gated on the per-guild config: that dial governs which channels transcribe *automatically*, and a DM has no guild row to read. The only gates are that faster-whisper is available and that the picked message carries audio. Attachment selection is deliberately wider than the listener's: any attachment whose `content_type` starts with `audio/` qualifies, falling back to the `IS_VOICE_MESSAGE` flag when Discord reports no content type. Someone who reached for the menu picked that message and meant it, so an uploaded `.mp3` is accepted where the automatic listener would ignore it.

The interaction is deferred (transcription exceeds Discord's 3s window) and the transcript is posted **publicly**, since the point is to leave the text in the conversation. Every failure mode replies **ephemerally** instead: unavailable, no audio found, transcription failed, or no speech detected. Model choice is the guild's configured model in a guild and `base.en` in a DM.

### Message length
Discord caps message content at 2000 characters, and the 1900-char `MAX_TRANSCRIPT_CHARS` budget counts the whole message — `📝 **{speaker}:** ` included — so both fitters take the prefix and subtract it rather than trusting the slack to absorb it. Both cut on a word boundary where one falls in the last fifth of the budget; a single word longer than the budget has no boundary to find and is cut mid-word.

The two paths then diverge, deliberately:

- **On demand (context menu)** — `split_transcript` spreads the transcript over as many messages as it takes. For a genuine voice note (`IS_VOICE_MESSAGE`) there is **no cap** on the number of parts: someone who explicitly pressed the button asked for the whole note. Only the first part carries the speaker prefix and no part is marked as a continuation: repeating `📝 **Name:**` would read as several separate notes rather than one that runs on.
  - An **uploaded audio file** is capped at `MAX_UPLOAD_PARTS` (10, around 25 minutes of speech). `_audio_attachment` accepts any `audio/*` attachment on purpose, so without a cap any member could long-press an hour-long podcast and have the bot post hundreds of messages — and a run that long outlives the 15-minute interaction token, failing part-way through with nothing to show the presser. The last allowed part is *fitted* rather than cut bare, so a capped transcript ends with the same truncation note the listener uses.
- **Automatic (listener)** — `fit_transcript` keeps to a single message, appending the truncation note when it trims. An auto-post nobody asked for should not be able to fill a channel, and a transcript that simply stopped mid-sentence would read as a failure, so the cut is announced. The note is paid for out of the budget rather than added on top.

`delete_after_transcribe` stands down whenever the fit truncated (`was_truncated`): the clip is the only copy of the part that did not fit, so an auto-post that could not carry the whole note leaves the audio in place and logs why. A whole transcript still authorises the delete as before.

Until 2026-09-03 the listener posted raw text with no fitter at all: any note over the cap was rejected by Discord with a 400 raised out of the listener, so a long auto-transcribed note produced **nothing** — no transcript, and (because the send raised before it) no `delete_after_transcribe`, which at least left the audio in place.

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

- `PUT /config/voice-transcription` — upsert the guild config; unknown model names fall back to the default. Turning the feature **on** with a model that isn't in the local cache is rejected (400) — models load offline, so an un-downloaded one would fail silently on every voice message. Saving with `enabled=false` is never blocked (a wiped cache must not trap an admin), and the check is skipped entirely when faster-whisper isn't installed.
- `POST /config/voice-transcription/download` — download a model into the local cache (blocking network fetch run off the loop; no-op if already cached). This is the dashboard's model-download widget.
- The `voice_transcription` section of the config payload reports `enabled`, `model_name`, `channel_ids`, faster-whisper `available`, and per-model `cached` status.

## Stored data

One table, `voice_transcription_config`, one row per guild: `guild_id`, `enabled`, `model_name`, `channel_ids` (comma-separated string), `delete_after_transcribe` (migration 199, default off).

**Nothing about a transcription is retained.** The audio is written to a temp file only because faster-whisper reads from a path, and it is unlinked in a `finally` whether or not the transcribe succeeded. The transcript's only home is the Discord message that carries it. This holds for DMs by construction as well as by intent: `events_cog.on_message` returns early on `not message.guild`, so DM messages are never ingested into `messages`, and `dm_audit_log` records actions rather than content. Downloaded model weights live on disk under `.cache/huggingface/hub`.

## Non-goals

- No live voice-channel transcription — posted audio only.
- Only one attachment per message is transcribed (Discord voice notes carry exactly one; the menu takes the first audio attachment it finds).
- English-only models (`*.en`); no language detection or multilingual support.
- The **listener** has no user-facing error messages — failures are log-only. The **context menu** does report failures, ephemerally, because someone is waiting on a press.
- `delete_after_transcribe` does not apply to the context menu: it transcribes what it was pointed at and leaves it alone. Deleting someone's message on their behalf is not something a long-press should do, and in a DM the bot could not do it anyway.
