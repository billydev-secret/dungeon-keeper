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
--   'auto_delete' — our own auto-delete sweep expiring a channel on a timer
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
-- only queries that use this column want the deleted minority.
CREATE INDEX IF NOT EXISTS idx_messages_deleted
    ON messages (guild_id, deleted_at)
    WHERE deleted_at IS NOT NULL;
