-- 153_xp_events_guild_created_index.sql
-- xp_events: index the (guild_id, created_at) shape the dashboard landing page
-- uses on every load.
--
-- home.py runs two "WHERE guild_id = ? AND created_at >= ?" aggregates for the
-- XP tile (SUM(amount) and COUNT(DISTINCT user_id) over the last 24 h). The
-- only existing indexes are (guild_id, source, created_at, user_id) and
-- (guild_id, channel_id, created_at) — created_at sits behind another column in
-- both, so SQLite could only seek on guild_id and then walked every row for the
-- guild (~1.02 M in prod at the time of writing).
--
-- Measured on a scratch copy of the prod table (1,023,043 rows):
--   SUM(amount)           58.5 ms -> 0.7 ms
--   COUNT(DISTINCT user)  48.6 ms -> 0.9 ms
-- and both plans change from "SEARCH ... (guild_id=?)" to
-- "SEARCH ... USING INDEX idx_xp_events_guild_created (guild_id=? AND created_at>?)".
--
-- Deliberately independent of the deferred xp_events retention/rollup work:
-- this only changes how the existing rows are reached, not how many are kept.

CREATE INDEX IF NOT EXISTS idx_xp_events_guild_created
ON xp_events (guild_id, created_at);
