-- Hidden-channel holds.
--
-- Records channels an admin has hidden with `/hidden hide`: the channel's
-- permission overwrites and original placement are snapshotted here, the
-- channel is denied to @everyone and parked under a "Hidden Channels"
-- category. `/hidden restore` reads the row back to move the channel home
-- and reinstate the exact overwrites.

CREATE TABLE IF NOT EXISTS hidden_channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    original_parent_id  INTEGER,           -- NULL if the channel was top-level
    original_position   INTEGER NOT NULL DEFAULT 0,
    stored_overwrites   TEXT NOT NULL DEFAULT '[]',  -- JSON [{id,type,allow,deny}]
    hidden_by       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    restored_at     REAL,
    status          TEXT    NOT NULL DEFAULT 'active'
);

-- At most one active hold per channel (enforces "already hidden" rejection).
CREATE UNIQUE INDEX IF NOT EXISTS idx_hidden_channels_active
    ON hidden_channels (guild_id, channel_id) WHERE status = 'active';
