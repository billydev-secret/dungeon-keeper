-- Repair schema drift: columns/indexes that only ever existed in in-code
-- init_* helpers, so a migrations-only build (fresh install / disaster
-- recovery) was missing them. All statements are idempotent — against a prod
-- DB that already has them via the init_* helpers they are harmless no-ops
-- (the runner tolerates "duplicate column name"; IF NOT EXISTS covers indexes).

-- ── auto_delete_rules.media_only (services/auto_delete_service.py) ─────
-- Only ever created by init_auto_delete_tables. Without it
-- should_track_auto_delete_message raises "no such column: media_only",
-- aborting the whole message-archive write.
ALTER TABLE auto_delete_rules ADD COLUMN media_only INTEGER NOT NULL DEFAULT 0;

-- ── user_interactions_log dedup + perf indexes (services/interaction_graph.py) ─
-- idx_interactions_log_dedup only ever existed in init_interaction_tables,
-- which has ZERO production callers. record_interactions' INSERT OR IGNORE +
-- rowcount==0 dedup depends on it; without it every backfill re-run
-- double-counts every edge. A UNIQUE index build fails if duplicates already
-- exist, so purge them first (keep the earliest row per dedup key), then
-- recompute the user_interactions aggregate from the deduped log so past
-- inflation is corrected. All real callers pass amount=1 + a message_id, so
-- COUNT(*) reproduces the true weight.
DELETE FROM user_interactions_log
WHERE message_id IS NOT NULL
  AND rowid NOT IN (
    SELECT MIN(rowid)
    FROM user_interactions_log
    WHERE message_id IS NOT NULL
    GROUP BY guild_id, message_id, from_user_id, to_user_id
  );

CREATE INDEX IF NOT EXISTS idx_interactions_log_guild_ts
ON user_interactions_log (guild_id, ts);

CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_log_dedup
ON user_interactions_log (guild_id, message_id, from_user_id, to_user_id)
WHERE message_id IS NOT NULL;

DELETE FROM user_interactions;

INSERT INTO user_interactions (guild_id, from_user_id, to_user_id, weight)
SELECT guild_id, from_user_id, to_user_id, COUNT(*)
FROM user_interactions_log
GROUP BY guild_id, from_user_id, to_user_id;

-- ── xp_events / member_xp / member_activity / role_events / processed_messages
-- indexes (core/xp_system.py). init_xp_tables has no src/ callers, so on a
-- migrations-built DB xp_events had NO index at all and get_xp_leaderboard
-- did a full SCAN of an ~888k-row table. Fold in every index the helper
-- defines. (messages is already indexed by 007/027/051 — left untouched.)
CREATE INDEX IF NOT EXISTS idx_xp_events_lookup
ON xp_events (guild_id, source, created_at, user_id);

CREATE INDEX IF NOT EXISTS idx_xp_events_channel
ON xp_events (guild_id, channel_id, created_at);

CREATE INDEX IF NOT EXISTS idx_member_xp_leaderboard
ON member_xp (guild_id, total_xp DESC);

CREATE INDEX IF NOT EXISTS idx_member_activity_guild_ts
ON member_activity (guild_id, last_message_at);

CREATE INDEX IF NOT EXISTS idx_role_events_lookup
ON role_events (guild_id, role_name, action, granted_at);

CREATE INDEX IF NOT EXISTS idx_processed_messages_backfill
ON processed_messages (guild_id, user_id, created_at, channel_id);
