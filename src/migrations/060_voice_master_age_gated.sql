-- Migration 060: add the "age_gated" flag to saved Voice Master profiles.
-- The owner-facing access control collapsed to a single 4-state dial:
--   open · NSFW (age-gated, open) · NSFW locked (age-gated + invisible) · spectator.
-- Everything but plain "open" is age-gated, so "locked" and "spectator" imply
-- age_gated at the application layer; this column is what distinguishes the
-- middle "NSFW but open" state from a plain open room.
ALTER TABLE voice_master_profiles ADD COLUMN age_gated INTEGER NOT NULL DEFAULT 0;
