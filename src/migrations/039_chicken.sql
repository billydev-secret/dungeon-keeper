-- Chicken (2..N) — mutual nerve / brinkmanship. All players hold; a shared meter climbs
-- 0→100 over climb_duration. Bailing removes you to safety (recorded with the meter % you
-- bailed at). Whoever is still holding at the crash loses; for the single-nick rail the
-- nick goes to one deterministic crasher, the rest are cosmetic co-losers. If everyone
-- bails first, the last to bail wins and no nickname is applied.
-- `alive` holds the still-holding set; bail_log is JSON [{player_id, bail_ts, meter_pct}].
CREATE TABLE IF NOT EXISTS chicken_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'LOBBY',
    phase TEXT,
    roster TEXT NOT NULL DEFAULT '[]',
    alive TEXT NOT NULL DEFAULT '[]',
    elimination_order TEXT NOT NULL DEFAULT '[]',
    bail_log TEXT NOT NULL DEFAULT '[]',
    winner_id INTEGER,
    loser_id INTEGER,
    stakes_text TEXT,
    message_id INTEGER,
    result_message_id INTEGER,
    climb_started_at REAL,
    climb_duration REAL,
    last_action_at REAL,
    resolved_at REAL,
    created_at REAL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS chicken_config (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    climb_duration REAL NOT NULL DEFAULT 25.0,
    min_players INTEGER NOT NULL DEFAULT 2,
    max_players INTEGER NOT NULL DEFAULT 8,
    lobby_timeout REAL NOT NULL DEFAULT 60.0
);
