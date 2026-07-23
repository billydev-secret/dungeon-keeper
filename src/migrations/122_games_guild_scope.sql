-- 122_games_guild_scope.sql
-- Scopes per-guild games tables by guild_id.
--
-- games_allowed_channels and games_game_history were keyed on row/channel id
-- alone, so the dashboard aggregated every guild's rows into one guild's view
-- (a game host of guild A could list/delete guild B's allowed channels and see
-- guild B's game history/stats). Add a guild_id discriminator to both.
--
-- Idempotent: the migration runner tolerates "duplicate column name", so a
-- re-run of ADD COLUMN is a no-op.

ALTER TABLE games_allowed_channels ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE games_game_history     ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_games_ac_guild ON games_allowed_channels(guild_id);
CREATE INDEX IF NOT EXISTS idx_games_gh_guild ON games_game_history(guild_id, ended_at DESC);

-- Single-guild backfill. The dashboard reads these tables filtered by the active
-- guild, so without a backfill every legacy row (all guild_id = 0) would vanish
-- from the games Channels / Stats / Logs panels until reconciled. When the
-- install has exactly ONE guild configured (games_game_config is correctly
-- per-guild), that guild owns every legacy row unambiguously, so assign it.
--
-- The `COUNT(DISTINCT guild_id) = 1` guard makes this self-limiting: on a
-- genuine multi-guild install it does nothing and the rows stay at guild_id = 0
-- (there is no reliable channel_id -> guild map in SQL, so guessing is unsafe —
-- a manual reconcile is the correct path there). Fresh installs have no config
-- rows, so the guard is false and this no-ops. Gameplay is unaffected either
-- way: check_allowed_channel treats guild_id = 0 as a wildcard.
UPDATE games_allowed_channels
   SET guild_id = (SELECT guild_id FROM games_game_config LIMIT 1)
 WHERE guild_id = 0
   AND (SELECT COUNT(DISTINCT guild_id) FROM games_game_config) = 1;

UPDATE games_game_history
   SET guild_id = (SELECT guild_id FROM games_game_config LIMIT 1)
 WHERE guild_id = 0
   AND (SELECT COUNT(DISTINCT guild_id) FROM games_game_config) = 1;

-- OPEN QUESTION (deliberately NOT changed here): games_question_bank and
-- legitlibs_templates are still global/unscoped. They may be intentionally
-- shared content (a single cross-guild library) or they may need per-guild
-- scoping like the two tables above. That is a product decision for a human to
-- rule on; see the review summary for the evidence on each.
