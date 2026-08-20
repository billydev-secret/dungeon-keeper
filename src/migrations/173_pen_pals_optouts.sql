-- 173_pen_pals_optouts.sql
-- Pen Pals: a durable "don't match me" that outlives the current chat.
--
-- pen_pals_pool is current-state only — one row per member waiting *right
-- now* — so leaving the pool was a deletion, not a preference: nothing
-- recorded that the member wanted to stay out. That was survivable while
-- expiry was a dead end, but since 40f9f5aa (2026-08-15) a closing session
-- returns both members to the pool, so leaving held only until your current
-- chat ended. A TGM member was re-pooled by the bot on 08-17 and matched
-- again on 08-19 with no `join` of her own anywhere in pen_pals_pool_events;
-- from where she sat, leaving the pool simply did not work.
--
-- One row per opted-out member. Its presence is the whole state: set by any
-- leave surface (panel button, /penpals leave, the closing DM's button),
-- cleared by joining. The row is kept rather than a column on pen_pals_pool
-- precisely because the pool row is the thing that keeps being deleted.
--
-- Extends migration 160's pool-event vocabulary with one reason:
--
--   action 'skip'  reason opted_out
--
-- recorded when an expiring or abnormally-closed session declines to re-pool
-- a member because of this flag. As with 160's other 'skip' reasons, nothing
-- moved — the row exists so that "the pool did not grow" is distinguishable
-- from "the pool grew silently".
--
-- Metadata only — who and when. **Preserved through an erasure**, unlike the
-- rest of Pen Pals' per-user data: this is a suppression record, and deleting
-- it would cause the very processing the member objected to. See
-- docs/data_register.md for the Art 17(3) statement.

CREATE TABLE IF NOT EXISTS pen_pals_optouts (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    at       REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
