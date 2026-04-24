-- Migration 001: ID remap table for dev/prod environment separation (spec §4.3)
-- Rebuilt from scratch on every dev startup — treated as a cache, not source of truth.

CREATE TABLE IF NOT EXISTS id_remap (
    kind        TEXT    NOT NULL,    -- 'channel' | 'category' | 'role' | 'bot_user'
    prod_id     INTEGER NOT NULL,
    dev_id      INTEGER,             -- NULL if unmatched; resolve_id returns None
    name        TEXT    NOT NULL,    -- name at time of mapping, for diagnostics
    parent_name TEXT,                -- category name for channels; NULL otherwise
    matched_at  TEXT    NOT NULL,    -- ISO timestamp
    PRIMARY KEY (kind, prod_id)
);
