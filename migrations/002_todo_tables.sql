CREATE TABLE IF NOT EXISTS todos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    added_by     INTEGER NOT NULL,
    task         TEXT    NOT NULL,
    created_at   REAL    NOT NULL,
    completed_at REAL,
    completed_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_todos_guild
    ON todos (guild_id, completed_at);
