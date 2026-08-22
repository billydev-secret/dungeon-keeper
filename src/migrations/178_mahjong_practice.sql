-- 178_mahjong_practice.sql
-- Meadow Mahjong AI seats (docs/plans/mahjong-bots.md stage 2).
-- One column: practice tables (one human + bots) hold no escrow and record
-- no results/stats — the flag is what the settle path and the tables report
-- branch on. Bot seats themselves need no schema: they are ordinary
-- mahjong_seats rows with per-table negative user_ids (plan B3), which the
-- existing NOT NULL and one-live-seat-per-member constraints accept and
-- which can never collide with a Discord snowflake. 177 is taken by the
-- game-night-feedback branch (duel nick stake), hence 178.

ALTER TABLE mahjong_tables ADD COLUMN practice INTEGER NOT NULL DEFAULT 0;
