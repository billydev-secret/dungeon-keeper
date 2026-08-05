# W4.5 sweep: test-suite quality + logging/PII — 2026-08-05/06

## Test suite

- **The sweep's job was already done by
  `docs/plans/test-suite-slim-and-remote-resilience.md`** (started 07-24):
  it diagnosed the same structural bloat this sweep hunts (mock-layer cog
  tests vs the cogs-are-glue rule, copy-pasted embed-contract tests,
  parametrize-collapsible clusters, the expensive default DB fixture) and
  stages 1–5 fix instances *and* defaults. Current state confirms
  progress: only 4 mock-heavy cog test files remain at tests/ root, the
  shared embed-accent contract table exists, remote-runner skip
  discipline is in place (asset-dependent tests skip, not return), and
  the scoped gate hard-fails new logic files with no mapped test.
- T1 (handoff): this review added new failing-test obligations —
  whisper A1 (two-guild forget-me repro) and privacy A1 (>32k-message
  purge) — both belong at the repo/service layer per the plan's own rule.
- No new sweep findings; the plan is the tracker of record.

## Logging / PII

- `log.txt`: **wipe-on-boot is deliberate** (`__main__.py:89`) +
  RotatingFileHandler (~2 MB, 1 backup). Minimal-retention by design.
  Note: the stream handler's copy lands in **journald** via systemd, so a
  durable trail *does* exist — `journalctl -u dungeon-keeper` — subject
  to journald's own retention. Worth one line in DEPLOYMENT.md so
  "log.txt is wiped" stops implying "history is gone" (the anon-audit
  migration comment and a memory both assumed no fallback).
- **PII in logs: essentially none.** Repo-wide scan found one
  content-adjacent line — `ollama_client.py:436`, debug-level, truncated
  to 120 chars, LAN-LLM prompts. Everything else logs ids/counts/names.
  L1 (nit): keep that line debug-only forever; add a comment saying so.
- The durable-audit gap that matters is not logging — it's the Image
  Guard log channels still at 0 (loose-ends §2), where enforcement
  actions on member content have no trail at all.

## Verdict — Wave 4.5 complete

No new findings beyond handoffs. Six sweeps produced: 1 dependency action
(aiohttp), 1 DB retention action (xp_events), 2 doc lines (journald,
ollama debug), and confirmations that security, reliability, and
test-quality posture are strong. Synthesis (W5) is next and last.
