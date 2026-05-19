CREATE TABLE IF NOT EXISTS quote_audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    quoter_id         INTEGER NOT NULL,
    quoted_user_id    INTEGER NOT NULL,
    quoted_message_id INTEGER NOT NULL,
    posted_message_id INTEGER NOT NULL,
    theme             TEXT    NOT NULL,
    font              TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quote_audit_log_guild
    ON quote_audit_log (guild_id, ts DESC);
