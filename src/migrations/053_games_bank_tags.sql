-- Migration 053: add JSON-array tags to the shared games question bank
-- (mirrors legitlibs_templates.tags). NSFW is now represented purely as the
-- reserved tag "nsfw". The legacy `category` column is intentionally retained
-- (SQLite DROP COLUMN is avoided) but is no longer read or written by bank
-- CRUD, question fetching, or game launches.
ALTER TABLE games_question_bank ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';

-- Backfill from the old category column. New rows already default to '[]';
-- only the nsfw rows need rewriting.
UPDATE games_question_bank SET tags = '["nsfw"]' WHERE category = 'nsfw';
UPDATE games_question_bank SET tags = '[]'       WHERE category = 'sfw';
