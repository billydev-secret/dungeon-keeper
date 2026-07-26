-- Migration 135: let one external bot be watched in several channels at once.
--
-- Migration 097 keyed the watch table UNIQUE(guild_id, bot_user_id) — one
-- channel per bot per guild. That silently capped tracking at a single
-- channel: Gamebot runs games wherever it's invited, and every game it hosted
-- outside the one watched channel was dropped on the floor, never banked and
-- never paid.
--
-- Widening the key to include channel_id makes a watch a (bot, channel) pair,
-- so the same bot can be tracked in as many channels as it plays in, and games
-- running concurrently in different channels each pay out independently. The
-- payout path needs no other change: every window is already sliced per
-- channel and every parse is a pure function of that channel's own history, so
-- N channels is just N independent scans.
--
-- Forward-only and idempotent: no rows change, only the uniqueness rule
-- relaxes. Re-running `/games track watch` for a bot in a channel it already
-- watches still updates that row in place rather than duplicating it.

DROP INDEX IF EXISTS idx_games_ext_watch_guild_bot;

CREATE UNIQUE INDEX IF NOT EXISTS idx_games_ext_watch_guild_bot_channel
    ON games_external_watch (guild_id, bot_user_id, channel_id);
