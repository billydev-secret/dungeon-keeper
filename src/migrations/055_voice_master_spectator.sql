-- Migration 055: add the "spectator" access mode to saved Voice Master profiles.
-- When set, the owner's channel opens to spectators (join + listen + read) who
-- cannot speak, use video, or post in the side chat. Mutually exclusive with
-- "locked" at the application layer.
ALTER TABLE voice_master_profiles ADD COLUMN spectator INTEGER NOT NULL DEFAULT 0;
