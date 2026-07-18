# Implementation plan — Chat Revive session-gap model

**Status:** built 2026-07-18 (stages 0–3 landed together in one worktree).
Follow-on to `docs/plans/chat-revive.md` (v1 built 2026-07-14). Replaces the
rhythm-mode fire threshold only; every other gate (config, budget, guild-gap,
channel-rest, quiet-hours, liveness floor, cold-start fallback) is unchanged.

**Spec:** `docs/chat_revive_spec.md` — "how a lull is judged" + implementation
notes updated in the landing commit. Classification unchanged (Reference / built).

## Why (the defect)

v1 rhythm mode fired when `silence ≥ max(fire_multiplier × median_gap, p90_gap)`
(`fire_multiplier` default 4.0) over the distribution of **inter-message** gaps
in a 2-hour band. Chat is bursty: that distribution is dominated by tiny
intra-burst gaps (people replying in seconds during a live conversation), so
`median_gap` lands near 30–60 s and `4 × median` fires within ~4 minutes on a
lively channel. The trigger measured "a message wasn't followed quickly," not
"a conversation ended and didn't restart." Observed live: a revive 4 minutes
after the last message in a busy channel.

## The fix — model conversations, not messages

Segment each band's message stream into **conversations** (a silence >
`SESSION_GAP_SECONDS` ends one), learn the distribution of **between-conversation
gaps**, and fire when the current silence exceeds a high quantile of it. The
only remaining constant is the segmentation boundary (robust to its exact
value); the fire threshold is *learned* per band, not hand-picked.

Locked parameters (`chat_revive/logic.py`):
- `SESSION_GAP_SECONDS = 600` (10 min) — conversation boundary
- `INTERSESSION_QUANTILE = 0.90` — fire past the 90th-pct between-conversation gap
- `MIN_LULL_SECONDS = 900` (15 min) — absolute floor; never revive a warm channel
- `MIN_BAND_SESSIONS = 8` — fewer sampled conversation gaps → whole-day profile

## Stage 0 — calibration spike (done)

Read-only backtest against production `dungeonkeeper.db` for the complaining
channel + the next three busiest, per active band. Counts, over 60 days, how
many times each rule would cross its threshold (raw eagerness, ignores
budget/rest gates — comparable across rules). Script: scratchpad
`stage0_spike.py`.

Target channel (`1469…553`, 40.8k msgs/60d), **evening band 18:00–20:00**:

| rule | threshold | crossings/60d |
|---|---|---|
| v1 `4× median` | **~4 min** | **3950** |
| session `10m`, p90 | **38 min** | 145 |
| session `10m`, p95 | 51 min | 77 |

The second-busiest channel tracked the target almost exactly (p90 → 37 min).
Sparser channels scaled up naturally (1.7 h+); one low-activity evening band went
degenerate (~40 h) and simply self-suppresses — safe (raw crossings 19–25/60d
there, vs v1's 823). **Decision:** `SESSION_GAP=10m`, `Q=0.90`, `floor=15m`,
`MIN_BAND_SESSIONS=8` — turns the target's evening trigger from ~4 min → ~38 min,
~10× less eager, still fires on genuine lulls. p90 (not p95) keeps the busy-
channel threshold at a sensible ~38 min; `rest_hours` (8 h) + guild budget throttle
the rest.

## Stage 1 — pure math (done)

`chat_revive/logic.py`: `_session_threshold()` helper; `BandProfile` now carries
`fire_threshold / sessions_per_day / msgs_per_day / session_count`;
`compute_band_profiles` builds session profiles; `decide()` rhythm block fires at
`prof.fire_threshold × fire_multiplier`. `MIN_BAND_GAPS` removed. Coupled tests in
`tests/test_chat_revive_logic.py` rewritten — including a new bursty
`_evening_channel` fixture (the old perfectly-periodic 600 s one has *no*
between-conversation gaps under the new model).

## Stage 2 — service wiring + migration + spec (done)

`chat_revive_service.py` `refresh_rhythm`/`load_rhythm` read/write the new
columns; `ChannelConfig` + `evaluate()` fallback default drop 4.0 → 1.0.
**Migration 080** drops & recreates the recomputable `revive_channel_rhythm`
cache with the new columns and resets every `revive_channel_config.fire_multiplier`
to 1.0. Service/loop/web tests updated (loop's `_seed_lively_history` rebuilt
bursty so band 9 learns a ~1680 s threshold and the 3000 s trailing silence
clears it). Spec + this plan updated.

## Stage 3 — dashboard dial (done)

`fire_multiplier` repurposed from "× the inter-message median" (default 4.0,
2–10) to a **Patience ×** multiplier on the learned lull threshold (default 1.0,
range 0.5–3.0). Route `ChannelBody` Field + panel input range/label/tooltip
updated. Higher = wait longer; 1.0 = fire at the channel's own p90
between-conversation gap.

## Blast radius (as touched)

`chat_revive/logic.py` (math + `decide`), `chat_revive_service.py`
(`refresh_rhythm`/`load_rhythm` + `ChannelConfig`/`evaluate` defaults), migration
080, route `chat_revive.py` (dial validation), panel `chat-revive.js` (dial
input + header), spec, plan; tests `test_chat_revive_logic.py`,
`test_chat_revive_loop.py`. Service/actions/web-route tests needed no changes
(they assert structurally, not on the old field names).

## Future refinements (not built)

- Per-band **threshold cap** (e.g. 6 h) so degenerate low-activity bands revive
  a truly dead channel instead of never firing — deferred; degenerate bands
  self-suppressing is the safer default until live data says otherwise.
- Data-driven session boundary (bimodal-valley detection) instead of the fixed
  10 min — only if a channel's burst structure proves wildly different.
