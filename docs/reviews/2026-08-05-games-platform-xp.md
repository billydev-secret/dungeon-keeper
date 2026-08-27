# Games platform + XP — battery review, 2026-08-05

Bundle: `games/` (command_groups/constants/utils incl. game_manager,
question_source, ai_client), `games_{session,config,help}/`, `games_db`
tables, `scheduled_games_service`, `game_start_ping_service`; XP:
`xp_{service,cog}`, `message_xp_service`, `voice_xp_service`, 3 panels.
Spec: `games_system_spec.md` (corrected 2026-07-15, Reference).

## Architecture

- **A1 — dead structure**: `src/bot_modules/games_consent/` is an empty
  directory (no `__init__.py`, nothing) and the `games_consent` prod table
  (1 row) has **zero code references**. The 07-15 spec correction moved
  consent-gating to Roadmap; the scaffolding outlived the plan. Action:
  `rmdir` the directory now (trivial), drop the table in the next
  migration that touches games (don't burn a migration number on it
  alone; migration-collision hazard per memory). — **done: directory in
  `49a02867`, table in migration 184.**
- **A2 — `xp_reaction_awards` (40,879 rows) is missing from
  `purge_user_data`** while its siblings (`member_xp`, `xp_events`,
  `voice_sessions`) are purged. Clear oversight, one-line fix in the
  privacy-core A1/A2 batch. Register updated.
- Games platform shape (game_manager + question_source shared by the
  prompt games) is the consolidation point W3 batches should measure
  against — duplication findings there should propose moving code *here*.
- `games_game_history`: metadata only (host, counts) ✓.
  `legitlibs_templates`: member-authored template content with author_id —
  creative contribution, shared by design; note only.

## UX / Docs

- Member play in Discord, admin config on web (games-config panels,
  scheduled games) ✓. `games_system_spec` current per its 07-15 rewrite.
  No findings.

## GDPR

- XP family: activity-derived, purge-covered except A2's table.
  `econ_conversions` (XP→coin) covered by econ helper rec.
- `games_question_bank.added_by` / `revive_questions.created_by`:
  curator credit, innocuous — preserve.
- Processor: `games/utils/ai_client.py` (Anthropic) — inventoried in
  economy-sources G1; game AI-prep sends host-provided prompts, not
  member messages.

## Verdict

Two crisp actions: delete the dead consent scaffolding (A1) and add
`xp_reaction_awards` to the purge (A2). Otherwise clean.
