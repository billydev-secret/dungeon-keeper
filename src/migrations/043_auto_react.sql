CREATE TABLE IF NOT EXISTS auto_react_config (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    emojis     TEXT    NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, channel_id)
);
