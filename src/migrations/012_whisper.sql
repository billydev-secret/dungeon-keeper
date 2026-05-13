-- Migration 012: whisper anonymous-message guessing tables

CREATE TABLE IF NOT EXISTS whispers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    sender_id       INTEGER NOT NULL,
    target_id       INTEGER NOT NULL,
    message         TEXT    NOT NULL,
    created_at      REAL    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    solved          INTEGER NOT NULL DEFAULT 0,
    exposed         INTEGER NOT NULL DEFAULT 0,
    guesses_left    INTEGER NOT NULL DEFAULT 3,
    channel_msg_id  INTEGER,
    dm_msg_id       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_whispers_target
    ON whispers(guild_id, target_id, state);

CREATE INDEX IF NOT EXISTS idx_whispers_sender
    ON whispers(guild_id, sender_id);

CREATE TABLE IF NOT EXISTS whisper_guesses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    whisper_id  INTEGER NOT NULL REFERENCES whispers(id) ON DELETE CASCADE,
    guessed_id  INTEGER NOT NULL,
    correct     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whisper_guesses_whisper
    ON whisper_guesses(whisper_id);
