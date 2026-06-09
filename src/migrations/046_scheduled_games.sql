-- Scheduled games: auto-launch party games at set times (one-time / daily / weekly).
-- Wall-clock fields (time_of_day, recur_days, start_date) are the source of truth so a
-- later tz-offset change keeps the intended local time; next_run_at is a derived UTC-epoch
-- cache used by the polling loop.
CREATE TABLE IF NOT EXISTS games_scheduled (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    game_type        TEXT    NOT NULL,
    options          TEXT    NOT NULL DEFAULT '{}',
    created_by       INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    time_of_day      INTEGER NOT NULL,                 -- minutes since local midnight
    recurrence       TEXT    NOT NULL,                 -- 'once' | 'daily' | 'weekly'
    recur_days       TEXT,                             -- weekly: JSON weekday set [0..6], Mon=0
    start_date       TEXT,                             -- 'once': local YYYY-MM-DD anchor
    next_run_at      REAL,                             -- derived UTC-epoch cache (next fire)
    giveup_at        REAL,                             -- 'once' busy-retry deadline (UTC epoch)
    announce         INTEGER NOT NULL DEFAULT 0,
    announce_role_id INTEGER,
    status           TEXT    NOT NULL DEFAULT 'active', -- active | paused | done | cancelled
    last_run_at      REAL,
    last_status      TEXT                              -- launched|skipped_active|skipped_disabled|error|skipped_giveup
);

CREATE INDEX IF NOT EXISTS idx_games_scheduled_due
    ON games_scheduled(status, next_run_at);
