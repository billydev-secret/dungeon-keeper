-- Migration 009: veil guess-the-member game tables

CREATE TABLE IF NOT EXISTS veil_rounds (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id                 INTEGER NOT NULL,
    submitter_id             INTEGER NOT NULL,
    answer_id                INTEGER NOT NULL,
    channel_id               INTEGER NOT NULL DEFAULT 0,
    message_id               INTEGER NOT NULL DEFAULT 0,
    crop_path                TEXT    NOT NULL DEFAULT '',
    crop_url                 TEXT    NOT NULL DEFAULT '',
    difficulty               TEXT    NOT NULL DEFAULT 'medium',
    candidate_count          INTEGER NOT NULL DEFAULT 0,
    reroll_count             INTEGER NOT NULL DEFAULT 0,
    allow_reuse              INTEGER NOT NULL DEFAULT 0,
    is_reuse                 INTEGER NOT NULL DEFAULT 0,
    original_round_id        INTEGER,
    reuse_blocked            INTEGER NOT NULL DEFAULT 0,
    created_at               REAL    NOT NULL,
    solved_at                REAL,
    solver_id                INTEGER,
    guesses_to_solve         INTEGER,
    unique_guessers_to_solve INTEGER,
    answer_optout            INTEGER NOT NULL DEFAULT 0,
    deleted_at               REAL
);

CREATE INDEX IF NOT EXISTS idx_veil_rounds_guild_created
    ON veil_rounds(guild_id, created_at);

CREATE INDEX IF NOT EXISTS idx_veil_rounds_submitter
    ON veil_rounds(submitter_id);

CREATE INDEX IF NOT EXISTS idx_veil_rounds_reuse
    ON veil_rounds(guild_id, allow_reuse, solved_at, reuse_blocked);

CREATE TABLE IF NOT EXISTS veil_guesses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    guesser_id      INTEGER NOT NULL,
    guessed_user_id INTEGER NOT NULL,
    correct         INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_veil_guesses_round
    ON veil_guesses(round_id);

CREATE INDEX IF NOT EXISTS idx_veil_guesses_guesser
    ON veil_guesses(guesser_id);

CREATE TABLE IF NOT EXISTS veil_optins (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    opted_in_at REAL    NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);
