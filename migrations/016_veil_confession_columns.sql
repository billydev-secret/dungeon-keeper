-- Migration 016: veil confession round support
-- round_type distinguishes photo rounds from text confession rounds.
-- confession_text stores the submitted text for confession rounds.
-- confession_prompt_text is reserved for future prompt-style confessions.

ALTER TABLE veil_rounds ADD COLUMN round_type            TEXT NOT NULL DEFAULT 'photo';
ALTER TABLE veil_rounds ADD COLUMN confession_text       TEXT NOT NULL DEFAULT '';
ALTER TABLE veil_rounds ADD COLUMN confession_prompt_text TEXT NOT NULL DEFAULT '';
