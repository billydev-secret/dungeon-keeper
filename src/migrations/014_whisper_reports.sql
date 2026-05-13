-- Migration 014: whisper report dedupe table

CREATE TABLE IF NOT EXISTS whisper_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    whisper_id  INTEGER NOT NULL REFERENCES whispers(id) ON DELETE CASCADE,
    reporter_id INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    UNIQUE(whisper_id, reporter_id)
);

CREATE INDEX IF NOT EXISTS idx_whisper_reports_whisper
    ON whisper_reports(whisper_id);
