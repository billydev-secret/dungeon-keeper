-- Migration 097: allow tracking more than one external game bot per guild.
--
-- The stage-1 collector (migration 056) held one watched (channel, bot) per
-- guild (guild_id was the PRIMARY KEY). To run e.g. Gamebot (CAH) and Cat Bot
-- at the same time — each with its own parser and economy payout — the watch
-- table becomes multi-row per guild, with a `kind` selecting the parser.
--
-- SQLite can't repoint a PRIMARY KEY in place, so rebuild + copy. The single
-- pre-existing row per guild is the CAH tracker (the only bot supported so
-- far), so it migrates to kind='gamebot_cah'. UNIQUE(guild_id, bot_user_id)
-- keeps "one watch per bot per guild" while allowing several bots.

CREATE TABLE IF NOT EXISTS games_external_watch_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    bot_user_id  INTEGER NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'gamebot_cah',
    enabled      INTEGER NOT NULL DEFAULT 1,
    set_by       INTEGER NOT NULL DEFAULT 0,
    set_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO games_external_watch_new
    (guild_id, channel_id, bot_user_id, kind, enabled, set_by, set_at)
SELECT guild_id, channel_id, bot_user_id, 'gamebot_cah', enabled, set_by, set_at
FROM games_external_watch;

DROP TABLE games_external_watch;
ALTER TABLE games_external_watch_new RENAME TO games_external_watch;

CREATE UNIQUE INDEX IF NOT EXISTS idx_games_ext_watch_guild_bot
    ON games_external_watch (guild_id, bot_user_id);
