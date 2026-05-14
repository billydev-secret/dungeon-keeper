-- Migration 018: whisper reply-level report table

CREATE TABLE IF NOT EXISTS whisper_reply_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reply_id    INTEGER NOT NULL REFERENCES whisper_replies(id) ON DELETE CASCADE,
    reporter_id INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    UNIQUE(reply_id, reporter_id)
);

CREATE INDEX IF NOT EXISTS idx_whisper_reply_reports_reply
    ON whisper_reply_reports(reply_id);
