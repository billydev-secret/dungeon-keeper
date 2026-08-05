# AI moderation + Rules Watch + sentiment — battery review, 2026-08-05

Bundle: `ai_mod_cog.py`, `rules_watch/` (monitor/scorer/ledger/alert/service,
2.1k), `ai_moderation_service.py`, `moderation.py`, `sentiment_service.py`,
`ollama_client.py`, `ai_config.py`; panels config-moderation/config-ai/
rules-watch×3/mod-warnings. Spec: `ai_moderation_spec.md` (current — names
the retired slash config commands and the web replacements ✓).
Prior art: `2026-07-01-rules-watch-followups.md` (window race, fixed),
`2026-07-20-rules-watch-tuning.md` (GPU model era) — open deferred items
there remain open; this review does not re-litigate them.

## Architecture

- `rules_watch/` is properly modular; the 07-01 race fix and 07-20 tuning
  work left it in good shape. Sentiment is VADER, lazy-loaded, local.
- **Processors: everything stays on-premises.** `ollama_client` targets
  llama-server on the LAN GPU box; host validation allowlists localhost +
  an explicit local suffix (`ollama_client.py:116-123`) — deliberate SSRF
  hygiene, worth citing in the security sweep as the pattern for any
  future fetcher. No cloud LLM anywhere in the moderation path.
- A1 (low): `rules_events` has no retention sweep (1,671 rows, indefinite).
  Not urgent at this volume; see G1 for the decision this needs.

## UX

- Philosophy-compliant: mod tools live in Discord (`/ai …`, `/watch …`,
  context-menu report), config lives on the web, old config commands
  retired. All inspection output ephemeral. No findings.

## Docs

- Spec current. The two review docs correctly cross-reference. INDEX ✓.

## GDPR

- **Lawful basis is legitimate-interest moderation** and processing is
  all-local — the cleanest possible processor story for LLM moderation.
- **G1 — `rules_events.excerpt` retains 240 chars of message content +
  matched phrase indefinitely, regardless of `message_storage_level`.**
  Justified as moderation evidence, but it's the one place content
  retention escapes the storage-level dial and it's not in
  `purge_user_data`. Decision needed: document as deliberate preserve
  (evidence) + consider a 180d sweep for *dismissed* events — a false
  positive's excerpt has no evidentiary value.
- G2 — `rules_labels` are mod-verdict training examples (indefinite).
  Fine as a dataset decision; add one line to the spec saying so.
- G3 — `/watch`: targeted per-member LLM screening, watcher-subscribed
  (2 rows live). Standard covert moderation; register: deliberate,
  document who can see the watch list (mods only ✓). Purge decision:
  rows should die when watched member is erased — add `watched_users`
  to `purge_user_data`'s list (it's keyed watched/watcher).
- G4 — `/ai review|query` run LLM analysis over a member's archive on
  demand. Since prod retains full content (privacy-core U1 context),
  these are powerful; admin-gated ✓, ephemeral ✓, not logged as new
  content ✓. No change requested.

## Verdict

No high findings. One decision package (G1/G3: rules_events dismissed-event
sweep + watched_users purge line), one spec line (G2). Deferred rules-watch
tuning items remain tracked in their own docs.
