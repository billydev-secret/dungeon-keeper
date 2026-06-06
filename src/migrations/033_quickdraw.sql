-- 033_quickdraw.sql
-- Quickdraw duel: first-to-react wins, false-starters lose.

CREATE TABLE IF NOT EXISTS quickdraw_games (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    challenger_id     INTEGER NOT NULL,
    target_id         INTEGER NOT NULL,
    state             TEXT    NOT NULL DEFAULT 'PENDING',
    qd_state          TEXT    NOT NULL DEFAULT 'WAITING',
    winner_id         INTEGER,
    loser_id          INTEGER,
    stakes_text       TEXT,
    message_id        INTEGER,
    result_message_id INTEGER,
    draw_delay        REAL,
    fired_at          REAL,
    last_action_at    REAL,
    resolved_at       REAL,
    created_at        REAL    DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS quickdraw_config (
    guild_id              INTEGER NOT NULL PRIMARY KEY,
    min_delay             REAL    DEFAULT 3.0,
    max_delay             REAL    DEFAULT 8.0,
    draw_window           REAL    DEFAULT 5.0,
    void_on_double_noshow INTEGER DEFAULT 1
);
