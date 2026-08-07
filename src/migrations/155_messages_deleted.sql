-- 155_messages_deleted.sql
-- Message archive: record *that* a message was deleted, and where from.
--
-- The messages table has always been a permanent local archive — Discord
-- delete events never removed rows, so sentiment, XP audits and mod review
-- survive a deletion (events_cog.on_raw_message_delete documents the
-- rationale). What was never recorded is the deletion itself, so a deleted
-- message sat in Message Search indistinguishable from a live one. That also
-- makes a Discord deep link a trap: the link renders, the message is gone.
--
-- deleted_at is NULL for a live message and a unix timestamp otherwise, so it
-- reads as a boolean (deleted_at IS NOT NULL) wherever only the fact matters
-- while still answering "how long did this stand before it was removed" —
-- which is the question a moderator reading a search hit actually has.
--
-- deleted_source names where the deletion came from, because routine churn and
-- a real removal are not the same event to a reader:
--   'discord'     — a delete event we observed and cannot attribute further
--                   (Discord's raw payload names no actor). A member deleting
--                   their own message, a mod removing one, and a member's
--                   privacy-panel purge all land here.
--   'auto_delete' — the scheduled auto-delete sweep expiring a channel on a
--                   timer. Narrower than "the bot deleted it": a mod-triggered
--                   bulk cleanup reuses the same sweep without a db handle, and
--                   channel.purge() doesn't go through it at all, so both of
--                   those land as 'discord'.
--
-- A member's privacy purge is deliberately NOT its own source:
-- privacy_cog._run_deletion is guaranteed never to open the DB (asserted by
-- test_no_mode_touches_the_database), and recording that a member exercised a
-- privacy control would be a new kind of data about them on a mod-visible
-- surface.
--
-- Attribution is claim-then-delete: our own paths stamp their ids *before*
-- calling the Discord API, so the raw event that follows finds deleted_at
-- already set. mark_messages_deleted only writes WHERE deleted_at IS NULL, so
-- first writer wins and the later generic event can never overwrite a specific
-- source.
--
-- Not backfilled. Messages deleted before this ships left no trace to recover;
-- they stay NULL, i.e. indistinguishable from live, which is the honest state.
-- Analytics deliberately keep counting deleted rows — this column is a
-- search/display concern, not a metrics one.

ALTER TABLE messages ADD COLUMN deleted_at     INTEGER;
ALTER TABLE messages ADD COLUMN deleted_source TEXT;

-- Partial index: the overwhelming majority of rows are live (NULL), and the
-- only queries that touch these columns want the deleted minority.
--
-- The indexed column is ts, not deleted_at. deleted_at is never a seek key —
-- every filter tests IS NULL / IS NOT NULL, which the partial predicate already
-- encodes — whereas all six sort orders end in ts, so indexing ts lets the sort
-- read straight off the index instead of building a temp b-tree over every
-- deleted row. Measured on a 639k-row corpus with 0.8% deleted: 1.9 ms -> 0.03 ms
-- for a first page, and 52 ms -> 0.2 ms for the matching COUNT.
--
-- deleted_source is deliberately NOT in the key. Adding it ahead of ts wins
-- 0.03 ms on the two source filters and loses both of the above, because the
-- index then can't serve the ordering for the unqualified case. With only the
-- deleted minority in the index, filtering source within it is already free.
CREATE INDEX IF NOT EXISTS idx_messages_deleted
    ON messages (guild_id, ts)
    WHERE deleted_at IS NOT NULL;
