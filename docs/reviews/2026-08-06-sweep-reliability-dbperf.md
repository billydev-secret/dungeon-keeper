# W4.5 sweep: reliability + DB/perf — 2026-08-05/06

## Reliability

- **External-dependency failure modes are uniformly defensive** (verified
  across bundles): Marqo → three-valued UNKNOWN, never collapsed; Ollama →
  `is_available()` + canned-text fallback; faster-whisper → cog skipped at
  setup if missing; Anthropic → APIError/Timeout caught, 502/503 to
  caller; Lavalink → managed subprocess w/ install guidance; Spotify →
  creds-checked. The Marqo missing-weights boot-time ERROR is the model
  (fail loud once, degrade per-image).
- **Persistent views**: 53 `add_view` registrations across cogs — button
  surfaces survive restarts as a broad practice (intake even normalizes
  custom_id keys for post-restart dispatch). ✓
- **In-flight game state is in-memory** (batch A note): a restart drops
  live party/prompt rounds. Accepted today; the parked snapshot/restore
  branch (loose-ends §4) is the mitigation if it starts to hurt.
- R1 (low): background loops come in three shapes (`@tasks.loop`, service
  `*_loop.py` create_task runners, scheduler classes). Exception handling
  is present in each spot-checked, but there's no single pattern; a shared
  "resilient loop" helper (log + backoff + continue) would make the
  guarantee uniform instead of per-author.
- Restart-applies-changes model (systemd, user-pushed) is documented and
  respected by tooling (per-boot static cache-busting). ✓

## DB / performance

- 740 MB WAL-mode DB, `busy_timeout=30s` centralized in `db_utils` with a
  documented short-timeout escape hatch for latency-sensitive writes
  (auction bids) — this is careful engineering. WAL file checkpointed
  small at inspection time. 186 user indexes; hot tables (messages ×8,
  reaction_log, interactions_log) are indexed for their query patterns.
- **P1 — `xp_events` is the largest table (1,014,344 rows, ~170k/mo since
  2026-02)** and grows unboundedly while `member_xp` already holds
  aggregates. Recommendation: retain 90 days of events (recent-activity
  queries), roll older into monthly aggregates or delete — halves the
  DB's largest object with no feature loss. **Priority: medium.**
- P2: `message_sentiment`/`processed_messages`/`message_mentions`/
  `message_reactions` (~1.5M rows combined) ride the messages archive
  decision — if the guild ever drops storage-level from `all`, these
  derived tables stay useful; no action now.
- P3 (info): `econ_ledger` lacks a created_at index; current readers
  (tuning report, metrics) are offline/scan-tolerant. Add only if a
  panel starts querying by time window.

## Verdict

One medium action (P1 xp_events retention), one uniformity suggestion
(R1). The failure-mode and locking discipline are already house
strengths.
