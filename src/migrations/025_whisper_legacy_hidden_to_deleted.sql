-- Migration 025: migrate legacy whisper state='hidden' rows into the new
-- soft-delete column added in migration 024. Without this, pre-migration
-- hidden whispers are stranded in the DB — the inbox queries filter by
-- state IN ('pending','shared') and the hidden-inbox UI no longer exists.

UPDATE whispers SET deleted_at = created_at WHERE state = 'hidden' AND deleted_at IS NULL;
UPDATE whispers SET state = 'pending' WHERE state = 'hidden';
