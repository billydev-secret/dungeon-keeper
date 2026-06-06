-- Musical Chairs (3..N) — lobby + reflex elimination. chairs = players − 1 each round.
-- MUSIC (hidden duration) → SCRAMBLE (race to sit) → eliminate the unseated → repeat.
-- roster/alive/elimination_order/seated are JSON TEXT columns. host_id doubles as
-- challenger_id for the inherited nickname-stake flow.
CREATE TABLE IF NOT EXISTS mc_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'LOBBY',
    phase TEXT,
    round INTEGER NOT NULL DEFAULT 0,
    chairs INTEGER,
    roster TEXT NOT NULL DEFAULT '[]',
    alive TEXT NOT NULL DEFAULT '[]',
    elimination_order TEXT NOT NULL DEFAULT '[]',
    seated TEXT NOT NULL DEFAULT '[]',
    winner_id INTEGER,
    loser_id INTEGER,
    stakes_text TEXT,
    message_id INTEGER,
    result_message_id INTEGER,
    phase_started_at REAL,
    phase_duration REAL,
    last_action_at REAL,
    resolved_at REAL,
    created_at REAL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS mc_config (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    min_music REAL NOT NULL DEFAULT 5.0,
    max_music REAL NOT NULL DEFAULT 15.0,
    scramble_window REAL NOT NULL DEFAULT 8.0,
    false_start_elim INTEGER NOT NULL DEFAULT 1,
    min_players INTEGER NOT NULL DEFAULT 3,
    max_players INTEGER NOT NULL DEFAULT 10,
    lobby_timeout REAL NOT NULL DEFAULT 60.0
);
