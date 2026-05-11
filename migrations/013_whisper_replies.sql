-- Migration 013: whisper anonymous-reply chain table

CREATE TABLE IF NOT EXISTS whisper_replies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    whisper_id    INTEGER NOT NULL REFERENCES whispers(id) ON DELETE CASCADE,
    from_user_id  INTEGER NOT NULL,
    to_user_id    INTEGER NOT NULL,
    content       TEXT    NOT NULL,
    created_at    REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whisper_replies_whisper
    ON whisper_replies(whisper_id);
