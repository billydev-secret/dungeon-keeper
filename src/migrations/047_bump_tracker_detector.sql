ALTER TABLE bump_tracker_sites ADD COLUMN detector_bot_id  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bump_tracker_sites ADD COLUMN detector_pattern TEXT    NOT NULL DEFAULT '';
