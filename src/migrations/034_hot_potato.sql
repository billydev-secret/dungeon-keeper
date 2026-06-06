CREATE TABLE IF NOT EXISTS hot_potato_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    challenger_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    holder_id INTEGER,
    winner_id INTEGER,
    loser_id INTEGER,
    stakes_text TEXT,
    message_id INTEGER,
    result_message_id INTEGER,
    timer_seconds REAL,
    started_at REAL,
    pass_log TEXT NOT NULL DEFAULT '[]',
    last_action_at REAL,
    resolved_at REAL,
    created_at REAL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS hot_potato_config (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    min_timer REAL NOT NULL DEFAULT 10.0,
    max_timer REAL NOT NULL DEFAULT 45.0
);
CREATE TABLE IF NOT EXISTS hot_potato_style (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    total_points INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
