-- 124_legitlibs_template_guild.sql
-- Scope LegitLibs templates by guild, with guild_id = 0 meaning the shared
-- global pool.
--
-- legitlibs_templates had no guild_id, so every guild drew, listed, edited and
-- deleted one global set (migration 122 scoped the sibling games tables but
-- left this one as an explicit open question). Templates are now per-guild
-- (guild_id > 0 = owned by that guild) with an opt-in global tier (guild_id = 0
-- = shared with every guild). Selection draws the guild's own templates plus
-- the global ones.
--
-- Idempotent: the runner tolerates "duplicate column name", so a re-run of
-- ADD COLUMN is a no-op.

ALTER TABLE legitlibs_templates ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_ll_templates_guild
    ON legitlibs_templates(guild_id, tier, status);

-- Single-guild backfill. Existing templates are all global (guild_id = 0); on a
-- one-guild install that guild authored them, so assign them (they become
-- guild-owned and editable, and can be re-globalized individually). The
-- COUNT(DISTINCT) = 1 guard makes this self-limiting: a genuine multi-guild
-- install keeps them global (guessing an owner is unsafe), and a fresh install
-- has no config rows so this no-ops. Mirrors migration 122's backfill.
UPDATE legitlibs_templates
   SET guild_id = (SELECT guild_id FROM games_game_config LIMIT 1)
 WHERE guild_id = 0
   AND (SELECT COUNT(DISTINCT guild_id) FROM games_game_config) = 1;
