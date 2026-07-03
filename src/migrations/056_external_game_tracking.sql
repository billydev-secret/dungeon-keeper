-- Migration 056: track results from an external game bot (e.g. "Gamebot" CAH).
-- Our bot watches a configured channel + external bot user and banks every one
-- of that bot's messages RAW and unparsed into games_external_messages. Metrics
-- (wins, leaderboards, streaks) are derived later from this table, so we can
-- re-parse the full history when the external bot changes its format without
-- ever losing data. Idempotent on the source message_id.

-- Which external bot to watch, per guild. One watched bot+channel per guild.
CREATE TABLE IF NOT EXISTS games_external_watch (
    guild_id     INTEGER PRIMARY KEY,
    channel_id   INTEGER NOT NULL,
    bot_user_id  INTEGER NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    set_by       INTEGER NOT NULL DEFAULT 0,
    set_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Raw capture. content + embeds_json are the source of truth; parse_* columns
-- are filled in by the parser pass and can be reset to re-parse everything.
CREATE TABLE IF NOT EXISTS games_external_messages (
    message_id   INTEGER PRIMARY KEY,          -- source message id; dedup key
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    author_id    INTEGER NOT NULL,             -- the external bot's user id
    created_at   TIMESTAMP NOT NULL,           -- message.created_at, UTC ISO
    edited_at    TIMESTAMP,                    -- last edit we saw, if any
    content      TEXT NOT NULL DEFAULT '',
    embeds_json  TEXT NOT NULL DEFAULT '[]',   -- json list of embed.to_dict()
    parse_status TEXT,                         -- NULL=unparsed | 'ok' | 'skip' | 'error'
    parsed_at    TIMESTAMP,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_games_ext_msg_guild_time
    ON games_external_messages(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_games_ext_msg_parse
    ON games_external_messages(parse_status);
