-- Migration 011: store path to the submitter's original (uncropped) image
-- so the full picture can be revealed (spoilered) on first correct guess.
-- Path is cleared after the file is unlinked post-reveal.

ALTER TABLE veil_rounds ADD COLUMN original_path TEXT NOT NULL DEFAULT '';
