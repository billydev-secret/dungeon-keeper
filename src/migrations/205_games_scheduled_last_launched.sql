-- Migration 205: record when a schedule last actually launched
-- (2026-09-04, docs/reviews/2026-09-02-games-deep-review-findings.md platform-24).
--
-- games_scheduled kept only last_run_at / last_status — the last *poll* and
-- how it went — so the panel could say "skipped, channel was busy" for days
-- on end with no way to tell when the schedule had last produced a game.
-- Prod's daily Risky Rolls row read exactly that. The loop now stamps this
-- column on every successful launch and the panel shows it.
--
-- Holds no per-user data: a timestamp on a schedule. No data-register row.
--
-- Idempotent: the migration runner tolerates "duplicate column name".

ALTER TABLE games_scheduled ADD COLUMN last_launched_at REAL;
