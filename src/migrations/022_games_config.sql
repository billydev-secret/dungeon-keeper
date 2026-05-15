-- 022_games_config.sql
-- Per-guild enable/disable and options for each game type.

CREATE TABLE IF NOT EXISTS games_game_config (
    guild_id    INTEGER NOT NULL DEFAULT 0,
    game_type   TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    options     TEXT    NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, game_type)
);
