-- 045_bio_field_hint.sql
-- Per-field hint/example text shown in the bios wizard prompt to help
-- members know what to write.
ALTER TABLE bio_fields ADD COLUMN hint TEXT NOT NULL DEFAULT '';
