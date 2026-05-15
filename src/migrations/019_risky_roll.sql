-- Migration 019: Risky Rolls game state tables

CREATE TABLE IF NOT EXISTS risky_active_rounds (
    game_id            TEXT    PRIMARY KEY,
    channel_id         INTEGER NOT NULL,
    guild_id           INTEGER NOT NULL,
    opener_id          INTEGER NOT NULL,
    message_id         INTEGER,
    is_open            INTEGER NOT NULL DEFAULT 1,
    highest_user       INTEGER,
    lowest_user        INTEGER,
    reroll_user_ids    TEXT,
    auto_close_players INTEGER,
    auto_close_minutes INTEGER,
    created_at         REAL,
    skip_min_game_time INTEGER NOT NULL DEFAULT 0,
    second_lowest_user INTEGER,
    second_highest_user INTEGER
);

CREATE TABLE IF NOT EXISTS risky_round_rolls (
    game_id TEXT    NOT NULL,
    user_id INTEGER NOT NULL,
    roll    INTEGER NOT NULL,
    PRIMARY KEY (game_id, user_id),
    FOREIGN KEY (game_id) REFERENCES risky_active_rounds(game_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS risky_pending_questions (
    game_id              TEXT    PRIMARY KEY,
    channel_id           INTEGER NOT NULL,
    guild_id             INTEGER NOT NULL,
    winner_id            INTEGER NOT NULL,
    prompt_message_id    INTEGER,
    participant_user_ids TEXT    NOT NULL,
    lowest_tie_user_ids  TEXT,
    prompt_kind          TEXT    NOT NULL DEFAULT 'room',
    extra_questioner_id  INTEGER,
    questioners_asked    TEXT
);

CREATE TABLE IF NOT EXISTS risky_posted_questions (
    message_id        INTEGER PRIMARY KEY,
    channel_id        INTEGER NOT NULL,
    guild_id          INTEGER NOT NULL,
    asker_id          INTEGER NOT NULL,
    allowed_replier_ids TEXT  NOT NULL,
    question_text     TEXT    NOT NULL,
    asker_rolled_100  INTEGER NOT NULL DEFAULT 0,
    target_rolled_1   INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER
);
