-- 028_pressure_cooker.sql
-- Pressure Cooker duel game — schema

CREATE TABLE IF NOT EXISTS pressure_games (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    message_id        INTEGER,
    result_message_id INTEGER,
    challenger_id     INTEGER NOT NULL,
    target_id         INTEGER NOT NULL,
    stakes_text       TEXT,
    state             TEXT    NOT NULL,
    gauge             INTEGER DEFAULT 0,
    active_player     INTEGER,
    pumps_json        TEXT    DEFAULT '[]',
    winner_id         INTEGER,
    loser_id          INTEGER,
    stakes_honored    INTEGER,
    created_at        REAL    DEFAULT (unixepoch()),
    last_pump_at      REAL,
    resolved_at       REAL
);

CREATE INDEX IF NOT EXISTS idx_pg_guild_state ON pressure_games (guild_id, state);
CREATE INDEX IF NOT EXISTS idx_pg_state       ON pressure_games (state);
CREATE INDEX IF NOT EXISTS idx_pg_challenger  ON pressure_games (challenger_id);
CREATE INDEX IF NOT EXISTS idx_pg_target      ON pressure_games (target_id);

CREATE TABLE IF NOT EXISTS pressure_nicks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       INTEGER REFERENCES pressure_games (id),
    guild_id      INTEGER NOT NULL,
    loser_id      INTEGER NOT NULL,
    winner_id     INTEGER NOT NULL,
    original_nick TEXT,
    imposed_nick  TEXT    NOT NULL,
    applied_at    REAL    DEFAULT (unixepoch()),
    expires_at    REAL    NOT NULL,
    reverted_at   REAL,
    revert_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_pn_pending ON pressure_nicks (expires_at)
    WHERE reverted_at IS NULL;

CREATE TABLE IF NOT EXISTS pressure_cooldowns (
    guild_id     INTEGER NOT NULL,
    player_a     INTEGER NOT NULL,
    player_b     INTEGER NOT NULL,
    last_game_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, player_a, player_b)
);

CREATE TABLE IF NOT EXISTS pressure_config (
    guild_id           INTEGER PRIMARY KEY,
    cooldown_hours     INTEGER DEFAULT 48,
    sentence_hours     INTEGER DEFAULT 24,
    allow_early_revert INTEGER DEFAULT 0,
    channel_allowlist  TEXT    DEFAULT '[]',
    nick_denylist      TEXT    DEFAULT '[]',
    max_nick_length    INTEGER DEFAULT 32,
    max_stakes_length  INTEGER DEFAULT 200
);
