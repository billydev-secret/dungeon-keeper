-- 160_pen_pals_pool_events.sql
-- Pen Pals: an append-only record of every movement in and out of the pool.
--
-- pen_pals_pool is current-state only: a row per waiting member, deleted the
-- moment they are matched or leave. That made the two questions you actually
-- ask of a matchmaking pool unanswerable — "did these people join and drop
-- out, or were they matched?" and "how long has this pool been stuck?" — and
-- a TGM pool sitting at one member for five days went unnoticed because
-- nothing recorded that it had ever been larger (2026-08-14).
--
-- One row per mutation, with the path that caused it:
--
--   action 'join'   reason panel | command | dm | requeue_expired |
--                          requeue_abnormal | backfill
--   action 'leave'  reason panel | command | dm | matched | departed
--   action 'skip'   reason inactive
--
-- 'skip' is the odd one: nothing moved. It records that an expiring session
-- deliberately did *not* return someone to the pool, which is otherwise
-- indistinguishable from the old silent behaviour.
--
-- Metadata only — who and when, never message content. Erasable: see
-- docs/data_register.md.

CREATE TABLE IF NOT EXISTS pen_pals_pool_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    at       REAL    NOT NULL,
    action   TEXT    NOT NULL,
    reason   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pen_pals_pool_events_guild
    ON pen_pals_pool_events (guild_id, at DESC);
