-- Shared infrastructure for N-player (BaseGame) games.
-- Group games have no "pair", so cooldowns are tracked per individual player
-- (coexists with the pairwise duel_cooldowns table used by 2-player duels).
-- Group games reuse duel_config (cooldown_hours, sentence_hours, nick_denylist, …)
-- keyed by game_type; per-game knobs (min/max players, lobby timeout) live in each
-- game's own *_config table.
CREATE TABLE IF NOT EXISTS duel_group_cooldowns (
    guild_id     INTEGER NOT NULL,
    game_type    TEXT    NOT NULL,
    player_id    INTEGER NOT NULL,
    last_game_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, game_type, player_id)
);
