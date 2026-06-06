-- 032_duels.sql
-- Shared duel infrastructure: nicks, cooldowns, config across all duel game types.

CREATE TABLE IF NOT EXISTS duel_nicks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       INTEGER NOT NULL,
    game_type     TEXT    NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_dn_pending ON duel_nicks (expires_at)
    WHERE reverted_at IS NULL;

CREATE TABLE IF NOT EXISTS duel_cooldowns (
    guild_id     INTEGER NOT NULL,
    game_type    TEXT    NOT NULL,
    player_a     INTEGER NOT NULL,
    player_b     INTEGER NOT NULL,
    last_game_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, game_type, player_a, player_b)
);

CREATE TABLE IF NOT EXISTS duel_config (
    guild_id           INTEGER NOT NULL,
    game_type          TEXT    NOT NULL,
    cooldown_hours     INTEGER DEFAULT 48,
    sentence_hours     INTEGER DEFAULT 24,
    allow_early_revert INTEGER DEFAULT 0,
    channel_allowlist  TEXT    DEFAULT '[]',
    nick_denylist      TEXT    DEFAULT '[]',
    max_nick_length    INTEGER DEFAULT 32,
    max_stakes_length  INTEGER DEFAULT 200,
    PRIMARY KEY (guild_id, game_type)
);
