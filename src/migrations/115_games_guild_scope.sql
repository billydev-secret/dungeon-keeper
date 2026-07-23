-- 115_games_guild_scope.sql
-- Scopes per-guild games tables by guild_id.
--
-- games_allowed_channels and games_game_history were keyed on row/channel id
-- alone, so the dashboard aggregated every guild's rows into one guild's view
-- (a game host of guild A could list/delete guild B's allowed channels and see
-- guild B's game history/stats). Add a guild_id discriminator to both.
--
-- Idempotent: the migration runner tolerates "duplicate column name", so a
-- re-run of ADD COLUMN is a no-op.
--
-- Conservative backfill: existing rows keep guild_id = 0 (there is no reliable
-- channel_id -> guild_id map in the schema to resolve them inside a migration).
-- A one-time reconcile is needed to assign legacy rows to their real guild;
-- until then legacy allowed-channels/history rows are invisible in the
-- (now guild-scoped) dashboard. Gameplay is unaffected: the in-Discord
-- check_allowed_channel gate still matches globally-unique channel ids and
-- treats guild_id = 0 as a wildcard, so existing allowed channels keep working.

ALTER TABLE games_allowed_channels ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE games_game_history     ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_games_ac_guild ON games_allowed_channels(guild_id);
CREATE INDEX IF NOT EXISTS idx_games_gh_guild ON games_game_history(guild_id, ended_at DESC);

-- OPEN QUESTION (deliberately NOT changed here): games_question_bank and
-- legitlibs_templates are still global/unscoped. They may be intentionally
-- shared content (a single cross-guild library) or they may need per-guild
-- scoping like the two tables above. That is a product decision for a human to
-- rule on; see notes_for_reviewer on the fix/s2-games-guild-scope branch.
