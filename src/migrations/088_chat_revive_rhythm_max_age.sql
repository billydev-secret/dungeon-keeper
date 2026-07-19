-- Chat Revive: make the learned-rhythm cache staleness window configurable.
--
-- RHYTHM_MAX_AGE_SECONDS was a hardcoded module constant (6 hours) governing
-- how long the cached per-channel band profiles (learned lull thresholds)
-- are trusted before being recomputed from raw history. Default reproduces
-- the existing hard-coded behavior exactly.

ALTER TABLE revive_guild_config
    ADD COLUMN rhythm_max_age_seconds REAL NOT NULL DEFAULT 21600.0;
