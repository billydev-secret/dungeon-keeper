-- Beta tools: source-tag columns for clean cleanup of synthetic data.
-- Default NULL = real production data. 'beta_sim' = ambient sim. 'beta_seed' = one-shot historical seed.
-- More tables get this column in later beta_tools plans (008+) as those code paths are added.
ALTER TABLE messages ADD COLUMN source TEXT;
ALTER TABLE member_xp ADD COLUMN source TEXT;
ALTER TABLE jails ADD COLUMN source TEXT;
ALTER TABLE tickets ADD COLUMN source TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_source
  ON messages(source) WHERE source IS NOT NULL;
