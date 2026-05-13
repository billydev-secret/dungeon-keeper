-- Migration 004: starboard
CREATE TABLE IF NOT EXISTS starboard_config (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL DEFAULT 0,
    threshold  INTEGER NOT NULL DEFAULT 3,
    emoji      TEXT    NOT NULL DEFAULT '⭐',
    enabled    INTEGER NOT NULL DEFAULT 1
);

-- Tracks who has reacted with the configured emoji per message.
-- Used for accurate effective-count (excludes self-stars) without extra Discord API calls.
CREATE TABLE IF NOT EXISTS starboard_reactors (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id, user_id)
);

-- Tracks messages posted to the starboard to prevent duplicates and enable count updates.
CREATE TABLE IF NOT EXISTS starboard_posts (
    guild_id             INTEGER NOT NULL,
    original_message_id  INTEGER NOT NULL,
    starboard_message_id INTEGER NOT NULL,
    original_channel_id  INTEGER NOT NULL,
    author_id            INTEGER NOT NULL,
    star_count           INTEGER NOT NULL DEFAULT 0,
    created_at           REAL    NOT NULL,
    PRIMARY KEY (guild_id, original_message_id)
);
