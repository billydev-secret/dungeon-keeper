-- Migration 194: drop eight stored-but-never-read config columns
-- (2026-08-30 dashboard configuration IA audit, findings 45–48 and 58).
--
-- Each one is a dial that was schema'd for a feature nobody built, so it has
-- a default, a column, and no reader. The defect queue already removed them
-- from the defaults dicts the panels merge over, leaving a NOTE where each
-- used to sit; this drops the columns those notes describe.
--
-- 1. `duel_config.allow_early_revert` (45). Early nickname revert was never
--    built — no reader anywhere in src/, no surface to set it.
--
-- 2. `quickdraw_config.void_on_double_noshow` (46). The double-no-show void is
--    unconditional; the flag has never been consulted.
--
-- 3. `hp_group_config.shake_threshold` / `.pass_mode` (47). The shake
--    threshold is a hard-coded default parameter of `game.shake_emoji` and
--    passing is always clockwise. Their defaults even disagreed between the
--    SQL ('choose') and db.py ('clockwise'), which nothing could notice
--    because nothing read either.
--
-- 4. `lobby_timeout` in `hp_group_config`, `chicken_config` and `mc_config`
--    (48). Loaded and then discarded by every caller; stale-lobby cleanup
--    hard-codes its own 90s-since-last-action window in each
--    `fetch_sweepable_games`.
--
-- 5. `confession_config.max_attachments` (58). Confessions are submitted
--    through a Discord modal, which has no attachment path at all. It goes
--    from the service's own `_create_tables` DDL in the same commit, so the
--    migration-built schema and the in-code one stay in step.
--
-- All five duel/group config tables are **empty on the live server**, so those
-- seven columns discard nothing. `confession_config` holds 3 rows, every one
-- carrying the untouched default of 4. No index references any of the eight,
-- so DROP COLUMN is legal. Verified read-only against the live database before
-- this was written.
--
-- Deliberately out of scope: `pressure_config` — the pre-migration-032 shape
-- of `duel_config`, which carries the same dead `allow_early_revert` but is
-- itself a whole orphan table (zero readers, zero rows). Dropping a table is a
-- bigger call than dropping a column and it was not on the approved list.

ALTER TABLE duel_config DROP COLUMN allow_early_revert;

ALTER TABLE quickdraw_config DROP COLUMN void_on_double_noshow;

ALTER TABLE hp_group_config DROP COLUMN shake_threshold;
ALTER TABLE hp_group_config DROP COLUMN pass_mode;
ALTER TABLE hp_group_config DROP COLUMN lobby_timeout;

ALTER TABLE chicken_config DROP COLUMN lobby_timeout;

ALTER TABLE mc_config DROP COLUMN lobby_timeout;

ALTER TABLE confession_config DROP COLUMN max_attachments;
