-- Migration 209: practice hands write a result row (2026-09-04, games deep
-- review mahjong-152, docs/reviews/2026-09-02-games-deep-review-findings.md).
--
-- Migration 181 added hand timing (started_at / discards) so the
-- seconds-per-discard figure every projected hand length rests on could be
-- measured on real play — and then read it from nothing, while practice
-- hands (most of the traffic while the game is new) recorded no result at
-- all, so the figure kept its n=1. Practice hands now write the one
-- `mahjong_results` row, flagged here, and still write no seats, no stats
-- and no coins: bots plan B5 is about money, not telemetry. The dashboard's
-- pace summary counts real hands only (a bot's reaction delay is
-- configured, not human) and marks practice rows as such.
--
-- Per-user data: no new column names a member. `mahjong_results` is already
-- registered (winner_id anonymised on purge); docs/data_register.md's row
-- notes the practice flag.

ALTER TABLE mahjong_results ADD COLUMN practice INTEGER NOT NULL DEFAULT 0;
