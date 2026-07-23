-- Daily snapshot of each community quest's running total, captured at the
-- guild-local day roll for the day that just ended. Community progress is
-- stored only as a single cumulative `current` per quest, so there is no way
-- to answer "how much did this goal move yesterday" after the fact — this
-- table is that history. Diffing two consecutive days' snapshots yields the
-- per-day gain the login digest shows as "biggest movers yesterday".
--
-- Written before any weekly settlement zeroes `current`, keyed by
-- (quest_id, day) so a crash-and-replay of the day roll overwrites the same
-- row rather than double-counting.
CREATE TABLE IF NOT EXISTS econ_community_progress_snapshots (
    guild_id  INTEGER NOT NULL,
    quest_id  INTEGER NOT NULL,
    day       TEXT    NOT NULL,
    current   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (quest_id, day)
);

CREATE INDEX IF NOT EXISTS idx_community_snap_guild_day
    ON econ_community_progress_snapshots (guild_id, day);
