-- 179_mahjong_hand_timing.sql
-- Meadow Mahjong: how long a hand actually takes, on real tables.
--
-- Simulation says a wall game is exactly 100 discards and a winning hand
-- ~70% of the live wall, and the first real game on prod matched that
-- exactly. What simulation cannot supply is the *clock*: the only estimate
-- we have of seconds-per-discard comes from one 78-minute game, and every
-- projected hand length scales linearly off it.
--
-- Two columns on the result, one on the table:
--   mahjong_results.started_at  when the hand was dealt (settle - start =
--                               duration; paired with discards it gives
--                               seconds per discard)
--   mahjong_results.discards    tiles thrown that hand, so the wall-game
--                               constant and the win-length curve can be
--                               checked against real play rather than bots
--   mahjong_tables.hand_started_at
--                               where the deal time is held until settle.
--                               It lives here and not in the engine state
--                               because the engine is deliberately free of
--                               the clock (parent plan D14) — deal() takes an
--                               injected wall and no timestamp, and that is
--                               what makes it replayable.
--
-- No new table and no new per-user column, so docs/data_register.md needs no
-- new row: these are properties of a hand, and the members who played it are
-- already recorded in mahjong_result_seats. Practice tables still record
-- nothing (178) — deliberately, since their seats are mostly bots whose
-- reaction delay is configured rather than human, so their timings would
-- answer a different question than the one being asked.
--
-- Both result columns are nullable: hands settled before this migration
-- have no timing, and reporting must treat NULL as "unknown", never zero.

ALTER TABLE mahjong_tables ADD COLUMN hand_started_at REAL;
ALTER TABLE mahjong_results ADD COLUMN started_at REAL;
ALTER TABLE mahjong_results ADD COLUMN discards INTEGER;
