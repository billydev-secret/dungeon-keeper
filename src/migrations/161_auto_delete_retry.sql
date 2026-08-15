-- 161_auto_delete_retry.sql
-- Bounded retry for the auto-delete queue.
--
-- A delete Discord refused with a transient error used to untrack the message,
-- which made it a permanent orphan: the sweep is queue-driven and the bounded
-- startup scan can't reach back past last_run_ts - max_age to rediscover it.
-- attempts/next_attempt_ts let a failure cost a backoff instead of the row.
--
-- Both default to 0 = "never failed, due now", so existing queues are unchanged
-- at deploy.
ALTER TABLE auto_delete_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE auto_delete_messages ADD COLUMN next_attempt_ts REAL NOT NULL DEFAULT 0;
