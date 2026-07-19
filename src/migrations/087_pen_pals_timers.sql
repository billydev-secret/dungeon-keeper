-- Pen Pals: make the pairing-mechanic timers configurable per guild.
--
-- These were hard-coded module constants in pen_pals_cog.py. Defaults
-- reproduce the existing hard-coded behavior exactly (no behavior change
-- for guilds that don't touch the new dashboard fields):
--   * session_seconds           -- how long a pairing session lasts (was
--                                   24 * 3600)
--   * match_cooldown_seconds    -- minimum gap before the same pair can be
--                                   re-matched (was 30 * 86400)
--   * max_question_swaps        -- max question re-rolls per session (was 3)
--   * warn_seconds               -- post the "session ending soon" warning
--                                   when this much time remains (was 3600)
--   * question_suppress_seconds -- skip posting a new auto-question if less
--                                   than this much session time remains
--                                   (was 2 * 3600)

ALTER TABLE pen_pals_config ADD COLUMN session_seconds INTEGER NOT NULL DEFAULT 86400;
ALTER TABLE pen_pals_config ADD COLUMN match_cooldown_seconds INTEGER NOT NULL DEFAULT 2592000;
ALTER TABLE pen_pals_config ADD COLUMN max_question_swaps INTEGER NOT NULL DEFAULT 3;
ALTER TABLE pen_pals_config ADD COLUMN warn_seconds INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE pen_pals_config ADD COLUMN question_suppress_seconds INTEGER NOT NULL DEFAULT 7200;
