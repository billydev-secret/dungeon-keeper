# Privacy core & data layer — battery review, 2026-08-05

Bundle: `cogs/privacy_cog.py`, `privacy/logic.py`, `services/privacy_service.py`,
`services/message_store.py`, `services/usage_telemetry_service.py` +
`cogs/usage_telemetry_cog.py`, `services/db_backup.py`. Spec: `privacy_spec.md`
(Reference). Tests: 5 files, logic-layer — coverage looks right.

## Architecture

- **Layering is textbook**: cog holds Discord I/O, `privacy/logic.py` holds
  pure decisions (mode matching, chunking, rendering), `privacy_service` holds
  the DB purge. Test files map cleanly. No findings on structure.
- **A1 — placeholder blowout in `purge_user_data`**
  (`privacy_service.py:46`): `msg_ids` is inlined as `IN (?,?,…)`. SQLite's
  default max is 32,766 variables; prod's top poster almost certainly exceeds
  that (631k message rows total). A real erasure run for an active member
  would raise `too many SQL variables` mid-purge, after some tables are
  already deleted, with no transaction wrapper visible at this layer —
  partial-purge state. Fix: chunk the id list (500s) or delete via
  `WHERE message_id IN (SELECT message_id FROM messages WHERE …)` before
  deleting the parent rows. **Priority: high** — this is the legal-erasure
  path failing exactly when it matters (heavy users).
- **A2** — inconsistent schema tolerance: wellness deletes are try/excepted,
  the other ~20 aren't; one renamed table aborts the purge partway. Wrap the
  whole loop uniformly (and run it in one transaction).
- **A3** — purge never checkpoints/VACUUMs; erased rows persist in WAL +
  freelist. For a legal erasure, follow with `PRAGMA wal_checkpoint(TRUNCATE)`
  + periodic VACUUM (document in the runbook rather than automating).
- A4 (minor) — `_delete_discord_messages` is ~140 lines of loop in the cog
  file; it's Discord I/O so it belongs there, but the per-channel body could
  drop a level (extract thread-archive dance). /simplify: **not applied** —
  the file is prod-critical and well-tested at the logic layer; churn > win.

## UX

- **U1 — the retention disclosure understates what this guild keeps.**
  Confirm prompt + summary say records kept are "mostly ingest-time metadata,
  not content" — but prod runs `message_storage_level='all'`: **452,172 of
  631,355 archived messages have full content**. The hedge is accurate only
  for the default level. `guild_retains_content(conn, guild_id)` is available
  at command time — the prompt should branch: content-retaining guilds get
  "including the message text". This is a one-line honesty fix in
  `privacy/logic.py` render copy. **Priority: high** (safety-rule adjacent:
  a disclosure that isn't true is a preference that isn't enforced).
- U2 — `/delete_user` silently skips channels the bot can't read; summary
  shows failures but not skipped-channel count. Fine for members; a mod
  running a legal-adjacent erasure should see "N channels unreadable".
- Otherwise strong: real confirm gating, per-target lock, single-card
  progress with DM fallback, mode names that tell the truth on the button.

## Docs

- `privacy_spec.md` is accurate and current (verified against code —
  including the e63e728 unwiring and the preserve-list policy). INDEX
  classification (Reference) correct.
- **D1** — manual.html: verify the Help panel's privacy section states that
  server-side records are kept and (per U1) that content-retaining guilds
  keep text. Deferred to the docs sweep (bulk manual.html check in W5).
- D2 — spec's "Stored data" note should mention the prod-relevant fact that
  a guild *has* opted into `all` and what that changes (or at least stop
  implying `none` is the operative case everywhere).

## GDPR

- **G1 — the purge-decision policy is unenforced.** Spec:128 requires every
  new per-user table to either join `purge_user_data` or document why not.
  Register diff (prod schema vs purge list): **~115 non-empty tables carry
  user identifiers with no decision recorded** — all econ_* (48) and
  casino_* tables, `reaction_log` (183k rows), whispers/whisper_replies,
  confession_* (deanonymizing author ids), bios/bio_answers/bio_field_values,
  pen_pals_*, member_birthdays, jails, warnings, tickets, watched_users,
  no_contact_pairs, games_*/duels, voice_follow_log, guess_*, rules_events….
  Spec names a handful as deliberately preserved (dm_perms consent, guess,
  whisper, jail, confessions audit) — the other ~35 features never decided.
  **This register (data_register.md) is where each Wave-1/2
  bundle records that decision**; Wave 5 turns undecided rows into either
  purge additions or documented-preserve lines in privacy_spec.
- **G2 — no erasure runbook.** The out-of-band path is "an operator runs
  purge_user_data manually" — no documented invocation (connection, guild
  id, keep_messages choice, WAL checkpoint, Discord-side pass ordering).
  A `docs/` runbook (or `scripts/gdpr_erase.py --dry-run`) closes it; A1
  must land first.
- **G3 — subject access/export**: explicitly a non-goal in spec (deferred).
  With content retention **on** in prod, an export request would be awkward
  to refuse. Flag for Ben as a policy question, not a code defect.
- G4 — `usage_events` (678 rows): named in purge ✓, retained indefinitely
  by design, spec discloses. OK.
- G5 — `db_backup.py`: backups copy the whole DB — erased users persist in
  older backups. Standard practice is a documented backup-retention window
  (then purge ages out). Note for the runbook (G2).
- Register rows appended for: messages+children (content when level=all),
  usage_events, known_users, member_gender, anon_audit_log,
  user_interactions(+log), wellness_* (12), member_xp/xp_events,
  voice_sessions, member_activity, member_events, quality_score_leaves.

## Verdict

Feature itself is in excellent shape; the two high-priority items are the
erasure path breaking on heavy users (A1) and the disclosure/reality gap on
content retention (U1). G1 is the review-wide workstream this register was
built for.
