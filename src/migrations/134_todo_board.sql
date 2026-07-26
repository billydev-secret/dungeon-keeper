-- Sticky todo board placement, one row per guild. 0 = not posted.
CREATE TABLE IF NOT EXISTS todo_board (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL DEFAULT 0
);

-- Recurring todo definitions. Column names/semantics mirror games_scheduled so
-- scheduled_games_service.compute_next_run can be reused verbatim:
--   time_of_day  minutes since guild-local midnight
--   recur_days   weekly only: JSON weekday set [0..6], Mon=0
--   next_run_at  derived UTC-epoch cache of the next fire time
CREATE TABLE IF NOT EXISTS todo_recurring (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    task         TEXT    NOT NULL,
    description  TEXT,
    recurrence   TEXT    NOT NULL DEFAULT 'daily',
    time_of_day  INTEGER NOT NULL DEFAULT 0,
    recur_days   TEXT,
    status       TEXT    NOT NULL DEFAULT 'active',
    next_run_at  REAL,
    last_run_at  REAL,
    last_status  TEXT,
    created_by   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_todo_recurring_due
    ON todo_recurring (status, next_run_at);

CREATE INDEX IF NOT EXISTS idx_todo_recurring_guild
    ON todo_recurring (guild_id);

-- Provenance for rows spawned by a recurring definition: lets the board mark
-- them and lets the spawner see whether the last instance is still outstanding.
ALTER TABLE todos ADD COLUMN recurring_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_todos_recurring
    ON todos (recurring_id, completed_at);
