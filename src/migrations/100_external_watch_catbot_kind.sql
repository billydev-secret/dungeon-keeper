-- Migration 100: correct the parser kind of pre-existing external-bot watches.
--
-- Migration 097 rebuilt games_external_watch with a `kind` column and defaulted
-- every pre-existing row to 'gamebot_cah', on the assumption that the only
-- external bot tracked so far was Gamebot Cards Against Humanity. In practice
-- the live watch points at Cat Bot (its Discord app id is the global constant
-- 966695034340663367), so that default would misroute Cat Bot's catch messages
-- to the CAH parser and silently drop Cat Bot payout.
--
-- Relabel any Cat Bot watch to 'catbot'. Forward-only fix (097 is already
-- released), idempotent, and a no-op on guilds that don't watch Cat Bot.

UPDATE games_external_watch
SET kind = 'catbot'
WHERE bot_user_id = 966695034340663367;
