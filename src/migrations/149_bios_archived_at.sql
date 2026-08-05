-- 149_bios_archived_at.sql
-- Bios: dedicated timestamp for the archived state (member left; embed
-- deleted, snapshot kept for rejoin resurrection). NULL means the bio is
-- live. Archival previously had no clock of its own — updated_at moves on
-- edits too — so stale-archive purging needs this column. Existing archived
-- rows (message_id/channel_id sentinel 0) are stamped at migration time so
-- their 12-month purge window starts now, not at their original leave date.

ALTER TABLE bios ADD COLUMN archived_at TEXT;

UPDATE bios
   SET archived_at = CURRENT_TIMESTAMP
 WHERE archived_at IS NULL
   AND message_id = 0
   AND channel_id = 0;
