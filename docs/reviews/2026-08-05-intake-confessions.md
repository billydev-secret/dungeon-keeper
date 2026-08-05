# Intake/greeting/welcome + Confessions/anon-audit — battery review, 2026-08-05

Two bundles, one doc (both small and clean).

## Bundle A: Intake cards / greeting watch / welcome

`intake_{service,loop,views,reference_service}.py` (2.2k),
`greeting_watch_{service,loop}.py` (533), `welcome_service.py` (107).
Specs: `intake_spec.md`, `greeting_watch_spec.md` — both Reference-grade,
verified current. 6 test files. Dashboard-only config, zero slash commands —
full philosophy compliance.

- **Architecture**: no findings. Snapshot-on-create card steps, live-config
  code matching with conflict validation at both writers, and the
  greeting-verdict loop reading `user_interactions_log` instead of storing
  text are all sound designs. `welcome_service` is pure formatting, stores
  nothing.
- **UX**: no findings. (intake-steps rollback SQL in repo root is the 08-01
  config edit's undo — covered by loose-ends §5.)
- **GDPR**:
  - `greeting_watch` stores **ids + timestamp only, no message text** —
    spec-documented, by design, at ingest. ✓ Rows are verdict-marked and
    kept (327 in prod). G1 (low): GC verdicted rows after ~30d; they have
    no ongoing use.
  - `intake_cards`/`intake_card_steps` (26/118 rows): newcomer progress +
    `done_by` greeter ids. Closed cards retained indefinitely. G2 (low):
    either add to `purge_user_data` or record a preserve line (greeter
    accountability). Register row updated → needs decision, low priority.
  - `econ_intake_rewards` covered under the economy register row.

## Bundle B: Confessions + anon audit

`confessions_cog.py` (878), `confessions_service.py` (559),
`anon_audit_service.py` (383). Specs both current.

- **Architecture**: no findings; purge paths are wired and *verified live in
  prod*: `confession_threads` TTL purge (7 days — oldest row is exactly
  2026-07-29) and `anon_audit_purge_loop` (configurable, default 90d; table
  is down to 7 rows). `purge_user_data` also clears anon_audit both
  directions (belt + braces).
- **GDPR — this is the best minimization story in the repo**: the
  author-id ↔ confession mapping lives **7 days** then is destroyed, so
  deanonymization is time-boxed by design rather than policy; ephemeral
  identity replies never write an assignment row at all. Register:
  confessions → **DECIDED** (TTL'd deanonymization + anon-audit sweep).
  Remaining crumbs, all trivial: `confession_rate_limits` (author ids +
  timestamps, self-expiring relevance), `confession_emoji_assignments`
  (identity-per-root, non-deanonymizing), config `blocked_user_ids`
  (mod blocklist — legitimate indefinite retention; documented in spec).
- **UX/Docs**: no findings; spec:113-117 "Stored data" is accurate,
  including the 7-day disclosure.
- Note for loose-ends §7: the unmerged `confessions-log-channel-optional`
  branch — this review found current confessions code healthy without it;
  ship-or-reap decision unaffected.

## Verdict

No high-priority findings in either bundle. Three low-priority GC/decision
items (greeting_watch GC, intake purge decision). Confessions' 7-day
deanonymization TTL joins Whisper/Wellness as a house pattern — the
synthesis should propose it for Guess's confession_text (currently
indefinite, see image-guard-guess G1).
