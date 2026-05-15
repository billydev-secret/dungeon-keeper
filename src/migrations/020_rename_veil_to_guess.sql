-- Migration 020: rename veil_* tables and indexes to guess_*

ALTER TABLE veil_rounds ADD COLUMN image_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE veil_rounds      RENAME TO guess_rounds;
ALTER TABLE veil_guesses     RENAME TO guess_guesses;
ALTER TABLE veil_optins      RENAME TO guess_optins;
ALTER TABLE veil_audit_log   RENAME TO guess_audit_log;

DROP INDEX IF EXISTS idx_veil_rounds_guild_created;
DROP INDEX IF EXISTS idx_veil_rounds_submitter;
DROP INDEX IF EXISTS idx_veil_rounds_reuse;
DROP INDEX IF EXISTS idx_veil_guesses_round;
DROP INDEX IF EXISTS idx_veil_guesses_guesser;
DROP INDEX IF EXISTS idx_veil_audit_guild_ts;
DROP INDEX IF EXISTS idx_veil_audit_round;

CREATE INDEX IF NOT EXISTS idx_guess_rounds_guild_created ON guess_rounds (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_guess_rounds_submitter     ON guess_rounds (submitter_id);
CREATE INDEX IF NOT EXISTS idx_guess_rounds_reuse         ON guess_rounds (guild_id, submitter_id, image_hash);
CREATE INDEX IF NOT EXISTS idx_guess_guesses_round        ON guess_guesses (round_id);
CREATE INDEX IF NOT EXISTS idx_guess_guesses_guesser      ON guess_guesses (guesser_id);
CREATE INDEX IF NOT EXISTS idx_guess_audit_guild_ts       ON guess_audit_log (guild_id, ts);
CREATE INDEX IF NOT EXISTS idx_guess_audit_round          ON guess_audit_log (round_id);
