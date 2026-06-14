-- 048_pen_pals.sql
-- Pen Pals: private 1-on-1 matched channels with prompted questions.

CREATE TABLE IF NOT EXISTS pen_pals_config (
    guild_id            INTEGER PRIMARY KEY,
    category_id         INTEGER NOT NULL DEFAULT 0,
    opt_in_role_id      INTEGER NOT NULL DEFAULT 0,
    question_category   TEXT    NOT NULL DEFAULT 'sfw',
    log_channel_id      INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 0,
    auto_round_dow      INTEGER NOT NULL DEFAULT -1,   -- 0=Mon … 6=Sun; -1=disabled
    auto_round_hour     INTEGER NOT NULL DEFAULT 12,   -- UTC hour
    last_auto_round_at  REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pen_pals_pool (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    joined_at REAL    NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS pen_pals_sessions (
    session_id          TEXT    PRIMARY KEY,
    guild_id            INTEGER NOT NULL,
    channel_id          INTEGER NOT NULL,
    user1_id            INTEGER NOT NULL,
    user2_id            INTEGER NOT NULL,
    started_at          REAL    NOT NULL,
    expiry_at           REAL    NOT NULL,
    next_question_at    REAL    NOT NULL,
    question_swaps_used INTEGER NOT NULL DEFAULT 0,
    close_warning_sent  INTEGER NOT NULL DEFAULT 0,
    closed_at           REAL,
    state               TEXT    NOT NULL DEFAULT 'active',
    close_reason        TEXT
);

CREATE INDEX IF NOT EXISTS idx_pen_pals_sessions_active
    ON pen_pals_sessions (state, expiry_at);

CREATE INDEX IF NOT EXISTS idx_pen_pals_sessions_channel
    ON pen_pals_sessions (channel_id);

CREATE INDEX IF NOT EXISTS idx_pen_pals_sessions_user
    ON pen_pals_sessions (guild_id, user1_id, user2_id);

CREATE TABLE IF NOT EXISTS pen_pals_questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL REFERENCES pen_pals_sessions(session_id),
    question_text TEXT    NOT NULL,
    shown_at      REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_pen_pals_questions_session
    ON pen_pals_questions (session_id);
