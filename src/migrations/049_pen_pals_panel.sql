-- 049_pen_pals_panel.sql
-- Add signup-panel tracking columns to pen_pals_config.

ALTER TABLE pen_pals_config ADD COLUMN panel_channel_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pen_pals_config ADD COLUMN panel_message_id INTEGER NOT NULL DEFAULT 0;
