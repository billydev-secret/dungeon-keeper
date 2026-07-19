# Game config knobs

**Status:** done · **Owner:** games/dashboard

## Goal

A dashboard-config audit found several game/feature cogs with hardcoded
rate-limit / cooldown / cap constants that should be per-guild dashboard
settings per CLAUDE.md's design philosophy ("configuration lives on the web
dashboard, not Discord"). Whisper's send cooldown + hourly cap were the first
fix (commit d3ff194); this doc tracks the rest, one commit per feature.

All of these follow the same shape: a Python module constant → a
`WhisperConfig`-style dataclass field, read/written through the existing
per-guild `config` key-value table (`get_config_value`/`set_config_value`,
no migration needed), exposed on the feature's existing (or, for LegitLibs, a
new) dashboard config panel, with tests proving the configured value —not the
old hardcoded default— drives behavior.

## Features

### 1. Whisper — done (d3ff194)
- `SEND_COOLDOWN_SECONDS` (30s), `SEND_PER_TARGET_HOURLY_CAP` (5/hr) →
  `WhisperConfig.cooldown_seconds` / `.hourly_cap_per_target`.

### 2. Confessions — `max_attachments` — SKIPPED, not a real gap
- Turned out to be dead config: confessions submit via `ConfessModal`, a
  Discord modal that only supports `TextInput` — there is no attachment path
  at all (`docs/confessions_spec.md`: "No attachment support today. Text
  bodies only."). `max_attachments` is stored/round-tripped but never read
  anywhere to enforce anything. Exposing it on the dashboard would ship an
  unenforced preference (CLAUDE.md: "Never ship a preference or toggle that
  isn't enforced"). Left alone — not worth removing the dead column either,
  out of scope for this sweep.

### 3. Guess — flood cap + per-round guess cap
- `src/bot_modules/cogs/guess_cog.py`: `_SUBMIT_WINDOW_S` (3600) /
  `_SUBMIT_MAX_PER_WINDOW` (5) — per-user submission flood cap.
  `MAX_GUESSES_PER_USER_ROUND` (5) — cap on guesses per user per round.
  Add both to whatever guess config dataclass/repo already backs
  `guess_cooldown_seconds`, expose on `config-guess.js`.

### 4. Risky Rolls — max concurrent games per channel
- `src/bot_modules/services/risky_roll/store.py`: `MAX_GAMES_PER_CHANNEL`
  (10), used in `risky_roll_cog.py`. Add alongside the existing
  `min_game_seconds` in `_risky_section`/`config-risky-rolls.js`.

### 5. Pen Pals — pairing-mechanic timers
- `src/bot_modules/cogs/pen_pals_cog.py`: `_MATCH_COOLDOWN_SECS` (30 days),
  `_SESSION_SECS` (24h), `_MAX_SWAPS` (3), `_WARN_SECS` (1h),
  `_Q_SUPPRESS_SECS` (2h). The existing Pen Pals panel only covers
  scheduling (day/hour/channels) — add a new section for these.
  (`_TICK_SECS`, `_RECENT_LIMIT` stay internal — not admin-tunable.)

### 6. Chat Revive — rhythm staleness window
- `src/bot_modules/services/chat_revive_service.py`: `RHYTHM_MAX_AGE_SECONDS`
  (6h). Lower priority/impact than the others.

### 7. LegitLibs — round timers — SCOPED DOWN
- The round timers (`FILL_TIMEOUT`/`CLAIM_TIMEOUT`/`RESCUE_TIMEOUT`/
  `POLL_INTERVAL` in `modes/classic.py`, `FILL_TIMEOUT` in `modes/quiplash.py`)
  are per-CHANNEL, not per-guild — a different shape from every other item
  in this sweep — and there's no existing dashboard section for LegitLibs
  round timing to extend. Building that means designing a new per-channel
  config UI from scratch. Discussed with the user; decision: skip the round
  timers for now (candidate follow-up, own session).
- Along the way found a real bug instead: `legitlibs_channel_config.max_tier`
  (per-channel heat-tier cap, already read by `run_classic`/`run_quiplash`
  via `get_channel_max_tier`) had **no writer anywhere** — always silently
  fell back to the default (4), making the whole per-channel tier cap
  feature dead despite `docs/games_system_spec.md` describing it as
  dashboard-managed. Fixed: added a "LegitLibs Max Tier" selector per row on
  the existing Games Config → Allowed Channels table
  (`PUT /api/games/config/channels/{id}/legitlibs-max-tier`).

## Excluded (checked, not real gaps)

- Voice Master, Voice Transcription, Burst Ranking: already fully configurable
  or not rate-limit-shaped.
- `guess_cog.py`'s `_MAX_URL_BYTES` (25MB fetch ceiling): a technical safety
  limit, not an admin tuning knob — left hardcoded.

## Workflow

Working in worktree `worktree-game-config-knobs`; each feature is its own
commit with tests, gate run manually (ruff + pyright + scoped pytest) before
`commit --no-verify` per the project's known pre-commit-hook timeout issue.
Merge back to main when all features are done and the user has reviewed.
