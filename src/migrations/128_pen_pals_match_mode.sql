-- Pen Pals: per-guild pairing mode — 'instant' (match the moment someone
-- joins, the long-standing default) or 'scheduled' (queue everyone and pair
-- the whole pool once a day at 8am Eastern).
ALTER TABLE pen_pals_config ADD COLUMN match_mode TEXT NOT NULL DEFAULT 'instant';
