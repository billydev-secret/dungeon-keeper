-- Migration 005: music cog (24/7 channel settings)
-- Per-voice-channel music settings. Queue state is in-memory only (v1).
CREATE TABLE IF NOT EXISTS music_channel_settings (
    guild_id              INTEGER NOT NULL,
    voice_channel_id      INTEGER NOT NULL,
    always_on             INTEGER NOT NULL DEFAULT 0,
    autoplay_playlist_url TEXT,
    last_updated_ts       INTEGER NOT NULL,
    updated_by_user_id    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, voice_channel_id)
);

-- Partial index speeds up startup rejoin scan (only 24/7 rows are interesting).
CREATE INDEX IF NOT EXISTS idx_music_always_on
    ON music_channel_settings(guild_id, always_on)
    WHERE always_on = 1;
