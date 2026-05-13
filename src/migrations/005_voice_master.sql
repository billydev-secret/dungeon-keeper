-- Migration 005: voice master
-- Member-owned voice channels created by joining a designated Hub voice channel.

CREATE TABLE IF NOT EXISTS voice_master_channels (
    channel_id      INTEGER PRIMARY KEY,
    guild_id        INTEGER NOT NULL,
    owner_id        INTEGER NOT NULL,
    created_at      REAL    NOT NULL,
    last_edit_at_1  REAL    NOT NULL DEFAULT 0,
    last_edit_at_2  REAL    NOT NULL DEFAULT 0,
    owner_left_at   REAL                      -- NULL while owner is in channel
);
CREATE INDEX IF NOT EXISTS idx_vm_channels_guild_owner
    ON voice_master_channels(guild_id, owner_id);

CREATE TABLE IF NOT EXISTS voice_master_profiles (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    saved_name  TEXT,                                  -- NULL = use template
    saved_limit INTEGER NOT NULL DEFAULT 0,            -- 0 = no cap
    locked      INTEGER NOT NULL DEFAULT 0,
    hidden      INTEGER NOT NULL DEFAULT 0,
    bitrate     INTEGER,                               -- NULL = guild default
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS voice_master_trusted (
    guild_id  INTEGER NOT NULL,
    owner_id  INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    added_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, owner_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_vm_trusted_target
    ON voice_master_trusted(guild_id, target_id);

CREATE TABLE IF NOT EXISTS voice_master_blocked (
    guild_id  INTEGER NOT NULL,
    owner_id  INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    added_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, owner_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_vm_blocked_target
    ON voice_master_blocked(guild_id, target_id);

CREATE TABLE IF NOT EXISTS voice_master_name_blocklist (
    guild_id INTEGER NOT NULL,
    pattern  TEXT    NOT NULL,
    added_at REAL    NOT NULL,
    added_by INTEGER NOT NULL,
    PRIMARY KEY (guild_id, pattern)
);
