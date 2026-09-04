-- Migration 204: stamp the guild on a live game at creation
-- (2026-09-04, docs/reviews/2026-09-02-games-deep-review-findings.md platform-19).
--
-- games_active_games never carried a guild, so end_game resolved one from the
-- bot's channel cache only when a bot was handed to it — and most end paths
-- (the daily photo post, lobby timeouts, empty-bank unwinds, crash cleanup)
-- pass none. Those archived guild_id = 0 into games_game_history: 51 prod rows
-- invisible to every guild-filtered dashboard query. Every launcher already
-- knows its guild, so record it once here and copy it on archive.
--
-- Holds no new per-user data: the column names a guild. No data-register row.
--
-- Idempotent: the migration runner tolerates "duplicate column name".

ALTER TABLE games_active_games ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0;

-- Rows live at the moment of the restart were created before the column
-- existed. The channel allowlist is keyed by channel_id and knows the guild
-- (migration 122), so backfill from it. A channel it doesn't know stays 0 and
-- end_game's own re-derivation handles that case.
UPDATE games_active_games
   SET guild_id = (
       SELECT guild_id FROM games_allowed_channels
        WHERE games_allowed_channels.channel_id = games_active_games.channel_id
        LIMIT 1
   )
 WHERE guild_id = 0
   AND EXISTS (
       SELECT 1 FROM games_allowed_channels
        WHERE games_allowed_channels.channel_id = games_active_games.channel_id
          AND games_allowed_channels.guild_id != 0
   );
