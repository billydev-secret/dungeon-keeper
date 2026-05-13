-- Migration 010: anonymous confession identity pools

ALTER TABLE confession_emoji_assignments ADD COLUMN name_index INTEGER NOT NULL DEFAULT -1;

CREATE TABLE IF NOT EXISTS confession_pools (
    guild_id        INTEGER NOT NULL,
    root_message_id INTEGER NOT NULL,
    pool_type       TEXT NOT NULL,
    remaining_json  TEXT NOT NULL DEFAULT '[]',
    cycle           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, root_message_id, pool_type)
);
