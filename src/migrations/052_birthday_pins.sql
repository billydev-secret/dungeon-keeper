-- Migration 052: track pinned birthday announcements so the next daily pass
-- can unpin them (~24h later). One row per pinned message.
CREATE TABLE IF NOT EXISTS birthday_pins (
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    pinned_date TEXT    NOT NULL,
    PRIMARY KEY (guild_id, channel_id, message_id)
);
