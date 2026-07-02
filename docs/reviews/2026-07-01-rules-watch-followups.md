# Rules Watch — investigation outcome & deferred work (2026-07-01)

Status: fixes committed on `96d3304` (needs bot restart to deploy). This note records
the root cause, what shipped, and the work intentionally deferred so it isn't lost.

Related: `docs/ai_moderation_spec.md`, `src/bot_modules/rules_watch/`.

## Background

Rules Watch is a passive moderation monitor: `on_message` → cheap gate → local LLM
guard (`ai_rules_watch_check`) → priority scorer → alert with ✅/❌ review buttons.
It was producing a flood of false positives — of 102 human-labeled events, **all 102
were false positives, 0 confirmed violations (~0% precision).**

## Root cause (the thing that mattered)

**A window race: the guard LLM never saw the message that triggered the alert.**

`RulesWatchMonitor._process` is dispatched via `asyncio.create_task` (monitor.py) and
reaches its DB window build with no `await`, while `events_cog` stores the incoming
message only *after* `await asyncio.to_thread(score_text, …)`. So the monitor
deterministically builds its conversation window **before** the triggering message is
in the `messages` table.

Verified empirically: the triggering message was **absent from its own `window_json`
in 12/12 recent events**. The gate fires on the incoming message (e.g. "slut" in a
Tenor URL), but the LLM then judges the *previous* 8 messages and flags something in
that unrelated banter — while the alert displays the trigger. Model and moderator were
looking at different text. The all-NULL `vader_compound` is the same race (sentiment
lookup for the not-yet-stored message returns nothing).

## Shipped in `96d3304`

1. **Window race fix** (`monitor.py`) — append the triggering message (from
   `message.content`, in memory) as the final window line before the guard call, so the
   LLM evaluates the message the alert is actually about.
2. **Slur gate split** (`scorer.py` `_get_slur_re`) — removed `slut`/`whore`/`cunt`
   from the gate list. On this kink-positive community these are consensual vocabulary
   (and appear in GIF/Tenor URLs); they drove most spurious gate-passes into a ~68s LLM
   call. Hard slurs (faggot / n-word / etc.) remain.
3. **Lenient guard parse** (`ai_moderation_service.py`) — strip ```fences and take the
   last flat `{…}`, so a fenced `"flag"` isn't silently dropped by a bare `json.loads`
   that defaults to `"ok"`.

## Deferred work (not done — intentionally)

### 1. Prompt re-validation, then possible swap
A precision-first rewrite of `_RULES_WATCH_SYSTEM` scored 30/30 real FPs cleared but
dropped recall (7–8/13 on seed violations). **Those numbers were measured on the buggy
(pre-fix) windows**, so they must be re-validated on *corrected* windows before the
prompt is swapped. Method: reconstruct corrected windows offline (append each stored
trigger message to its `window_json`), re-run current vs. precision prompt, compare
precision/recall. ~50 min on the prod box.

### 2. Move the LLM out of the message-receive path (architecture)
A CPU-only 3B at ~68s/inference does not belong synchronously in a real-time monitor.
Today `create_task` is unbounded and the single-worker inference executor queues
silently — on a busy evening the monitor becomes a stale-alert system with no
back-pressure or observability. Also, all AI features share ONE global model
(`ollama_client.chat`'s `model=` arg is ignored), so a slow manual `/ai review` blocks
Rules Watch entirely.

Target design: deterministic signals write a `rules_events` row and surface it for human
review immediately (e.g. `priority_tier = "needs_enrichment"`); the LLM runs as an
**async background enrichment job** that updates `guard_verdict`/`guard_confidence`/
`guard_rule` and re-scores priority, behind a **bounded queue / semaphore** that sheds
load and logs what it drops. Benefits: gate latency → 0, queue depth controlled, model
speed/quality tunable independently, and re-running enrichment against historical events
with a new prompt becomes possible.

### 3. Smaller known gaps
- **`detect_slur` misses obfuscation** (`f[a4]gg[o0]t` matches `faggot`/`f4gg0t` but not
  `f*ggot`/`f@ggot`). The gate therefore won't force the LLM on obfuscated hard slurs.
- **Labels are write-only.** `rules_labels` (`is_violation`, `corrected_rule`) is never
  read back into detection. Once precision is good enough to accumulate genuine positives,
  a few-shot-from-labels loop (inject confirmed examples into the prompt) is viable — this
  is the lightweight version of the "training system" that was asked about.
- **VADER gate clause is dead** while `vader_compound` is NULL (same race). Decide whether
  to compute sentiment inline for the trigger message; note this would *increase* gate
  pass-through / LLM load, so weigh against the precision goal.

## Tried and rejected (for the record)
- **Model swap.** Bigger models are off the table on the hardware (2-core embedded Ryzen
  R1600, CPU-only, ~68s/check on the current 3B; a 7–8B ≈ 3 min/check and is shared with
  interactive features). A same-size Qwen2.5-3B cleared only 33% of sampled FPs, lost a
  recall point, and was no faster. Not worth it.
