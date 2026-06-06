-- Hot Potato (group, 2..N) — generalizes the 2-player duel to a lobby + progressive
-- elimination loop. Roster/alive/elimination_order/pass_log are JSON TEXT columns
-- (same convention as hot_potato_games.pass_log). host_id doubles as challenger_id
-- for the inherited nickname-stake flow.
CREATE TABLE IF NOT EXISTS hp_group_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'LOBBY',
    round INTEGER NOT NULL DEFAULT 0,
    roster TEXT NOT NULL DEFAULT '[]',
    alive TEXT NOT NULL DEFAULT '[]',
    elimination_order TEXT NOT NULL DEFAULT '[]',
    holder_id INTEGER,
    winner_id INTEGER,
    loser_id INTEGER,
    stakes_text TEXT,
    message_id INTEGER,
    result_message_id INTEGER,
    fuse_seconds REAL,
    phase_started_at REAL,
    pass_log TEXT NOT NULL DEFAULT '[]',
    last_action_at REAL,
    resolved_at REAL,
    created_at REAL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS hp_group_config (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    min_fuse REAL NOT NULL DEFAULT 20.0,
    max_fuse REAL NOT NULL DEFAULT 60.0,
    min_hold REAL NOT NULL DEFAULT 2.0,
    shake_threshold REAL NOT NULL DEFAULT 0.70,
    pass_mode TEXT NOT NULL DEFAULT 'choose',
    min_players INTEGER NOT NULL DEFAULT 2,
    max_players INTEGER NOT NULL DEFAULT 10,
    lobby_timeout REAL NOT NULL DEFAULT 60.0
);
