-- 185_xp_rollup_watermark.sql
-- Stage 2 of docs/plans/xp-events-retention-and-rollup.md.
--
-- The readers that union xp_daily with raw xp_events need a partition point:
-- days at or before it come from the rollup, events after it come from raw.
-- Get that wrong in either direction and the answer is wrong — overlap
-- double-counts XP, a gap loses it.
--
-- The obvious partition point (a fixed "now - 90 days") is not safe, because
-- it assumes the rollup has actually covered everything older. It has not,
-- during the days after 151 ships while the loop backfills ~180 days of
-- history in chunks. A reader trusting a fixed boundary in that window would
-- quietly return partial all-time totals.
--
-- So the boundary is the rollup's own watermark: the newest day D such that
-- EVERY day with events up to and including D has been rolled up. Before any
-- backfill it is NULL and every reader falls back to raw-only — which is
-- exactly today's behavior, so Stage 2 is a no-op until the rollup has earned
-- the right to be read. It is also the interlock Stage 3 needs: never delete
-- raw rows for a day past the watermark.
--
-- Computing that watermark means scanning distinct days of a 1M-row table, so
-- it is stored rather than derived per query. One row, by construction.

-- Two facts, not one:
--   rolled_through_day — the end of the contiguous rolled prefix. Stage 3's
--                        interlock: never delete raw rows past this day.
--   first_gap_day      — the oldest day that HAS events and HAS NOT been
--                        rolled, or NULL for "no gaps". This is what the
--                        readers check, and it is not the same question.
--                        A watermark of 2026-01-17 sounds far behind a
--                        boundary of 2026-05-07, but if no events exist in
--                        between then the rollup covers everything below the
--                        boundary and is perfectly safe to read. Comparing
--                        the watermark to the boundary directly would refuse
--                        to read a complete rollup for a quiet guild.
CREATE TABLE IF NOT EXISTS xp_rollup_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    rolled_through_day TEXT,      -- UTC 'YYYY-MM-DD', or NULL for "nothing yet"
    first_gap_day      TEXT,      -- UTC 'YYYY-MM-DD', or NULL for "no gaps"
    updated_at         REAL NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO xp_rollup_state
    (id, rolled_through_day, first_gap_day, updated_at)
VALUES (1, NULL, NULL, 0);
