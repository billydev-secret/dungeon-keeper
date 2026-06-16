CREATE TABLE IF NOT EXISTS voice_transcription_config (
    guild_id    INTEGER PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 0,
    model_name  TEXT    NOT NULL DEFAULT 'base.en',
    -- Comma-separated channel allowlist. Empty = all channels (when enabled).
    channel_ids TEXT    NOT NULL DEFAULT ''
);
