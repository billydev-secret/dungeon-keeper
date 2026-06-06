-- 037_quickdraw_loser_time.sql
-- Quickdraw: record the loser's reaction time so the result can show the
-- player-vs-player delta ("Winner won by X.XXXs"). NULL when the loser never
-- fired (they ran out the draw window after the winner drew).
ALTER TABLE quickdraw_games ADD COLUMN loser_fired_at REAL;
