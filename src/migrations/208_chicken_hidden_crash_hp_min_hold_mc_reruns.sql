-- Migration 208: three party-game mechanics fixes from the games deep review
-- (2026-09-04, docs/reviews/2026-09-02-games-deep-review-findings.md
-- duels-party-113 / duels-party-114 / duels-party-119).
--
-- Chicken (113): the crash point used to be a fixed, public
-- `climb_duration`, so a table could count it out and the stake fired in
-- 4 games of 12. It is now rolled per game, hidden, between two dials:
-- `chicken_config.min_climb` / `max_climb` replace `climb_duration`, and
-- `chicken_games.crash_at` holds the rolled seconds-after-start. The meter
-- is drawn against `chicken_games.climb_duration`, which now stores the
-- game's max_climb so the bar can blow at 60%. A guild that had set
-- climb_duration keeps it as its ceiling.
--
-- Hot Potato duel (119): `hot_potato_config.min_hold` — the group cog's
-- anti-ping-pong wait, adopted by the duel so a pass is a decision.
--
-- Musical Chairs (114): `mc_games.reruns` counts consecutive rounds with no
-- sitter; the round re-runs once and voids on the second.
--
-- Per-user data: none new. Every table here is already registered under
-- the party-games row in docs/data_register.md and purged with it.
--
-- Idempotent on the ADDs: the migration runner tolerates
-- "duplicate column name".

ALTER TABLE chicken_config ADD COLUMN min_climb REAL NOT NULL DEFAULT 10.0;
ALTER TABLE chicken_config ADD COLUMN max_climb REAL NOT NULL DEFAULT 25.0;
UPDATE chicken_config
   SET max_climb = climb_duration,
       min_climb = MIN(10.0, climb_duration);
ALTER TABLE chicken_config DROP COLUMN climb_duration;

ALTER TABLE chicken_games ADD COLUMN crash_at REAL;

ALTER TABLE hot_potato_config ADD COLUMN min_hold REAL NOT NULL DEFAULT 2.0;

ALTER TABLE mc_games ADD COLUMN reruns INTEGER NOT NULL DEFAULT 0;
