-- 184_xp_daily_rollup.sql
-- Stage 1 of docs/plans/xp-events-retention-and-rollup.md.
--
-- xp_events is the largest table in the DB (1.02M rows, ~145MB with its two
-- indexes) and grows without bound, but it cannot simply be pruned: six
-- readers reach back further than any retention window worth having — the
-- per-source /xp leaderboards at year/alltime, the activity graphs at month
-- resolution (360 days), time-to-level, the mod profile's XP split, the /xp
-- existence gate, and the inactive report's unfiltered MAX(created_at).
--
-- This table is the aggregate those readers will union with the raw tail, so
-- that pruning old events changes what is stored without changing any answer.
-- Nothing reads it yet and nothing is deleted yet — Stage 1 is deliberately
-- inert so the numbers can be checked against live data before anything
-- depends on them.
--
-- Key notes:
--   * channel_id is in the key because the graphs filter on it, the exclusion
--     lists filter on it, and the inactive report groups by it. It stays
--     NULLable — 27% of existing rows have no channel (the column was added
--     later and backfilled only where processed_messages matched), and that
--     gap must survive rollup rather than be papered over.
--   * SQLite permits NULLs in a PRIMARY KEY, so the PK does NOT dedupe
--     NULL-channel rows. The writer must GROUP BY and replace wholesale;
--     it must never rely on ON CONFLICT to collapse them.
--   * first_at/last_at are kept per bucket: last_at is what serves the
--     inactive report's "when was this member last active here", and first_at
--     is what time-to-level needs for a member's first-ever event.
--   * day is UTC 'YYYY-MM-DD'. Local-time bucketing is a read-side concern
--     (only the hour-resolution graph does it) and storing local days would
--     make a guild's timezone change rewrite history.

CREATE TABLE IF NOT EXISTS xp_daily (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    source     TEXT    NOT NULL,
    channel_id INTEGER,
    day        TEXT    NOT NULL,
    xp         REAL    NOT NULL,
    events     INTEGER NOT NULL,
    first_at   REAL    NOT NULL,
    last_at    REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id, source, channel_id, day)
);

-- Mirrors idx_xp_events_lookup's shape: the leaderboard readers group by
-- user within (guild, source) over a day range.
CREATE INDEX IF NOT EXISTS idx_xp_daily_lookup
ON xp_daily (guild_id, source, day, user_id);

-- The inactive report's access path: last activity per member per channel.
CREATE INDEX IF NOT EXISTS idx_xp_daily_channel
ON xp_daily (guild_id, channel_id, day);
