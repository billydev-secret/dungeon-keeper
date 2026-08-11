-- Migration 158: the five windowed casino games become private, per-player
-- rounds instead of one communal round per channel.
--
-- WHY: roulette, derby, baccarat, dice and keno each posted a public board to
-- the casino channel, ran a betting-window countdown so others could join,
-- repainted the board on every bet, then played an animation and posted a
-- result. Measured over every round ever played in prod (218 of them), the
-- window bought almost nothing:
--
--     game      0 bettors   1 bettor   2+ bettors
--     roulette      4          43           9
--     derby         5          40          12
--     baccarat      3           8           6
--     dice          4          11           3
--     keno          5          57           8
--
-- 73% of rounds were one person playing alone in public, and 21 rounds drew
-- nobody at all — a board, a timer and an animation posted for zero players.
-- So the countdown mostly existed to let nobody join, while every one of those
-- messages scrolled the channel and buried the hub panel (each is an ordinary
-- bot message in a restick_on_bot channel; see
-- docs/reviews/2026-08-06-sticky-panel-machinery.md).
--
-- The round now belongs to the player who opened it and is paced by them: bet
-- as many times as you like, then press the game's resolve button. The
-- countdown is gone as a *betting deadline*.
--
-- WHAT `closes_at` MEANS NOW: an abandonment deadline, not a betting one.
-- Stake is debited when a bet is placed, and a private round renders in an
-- ephemeral message whose webhook token Discord expires after 15 minutes — so
-- a player who bets and wanders off would otherwise strand coins forever. The
-- existing maintenance sweep already resolves any round past its closes_at,
-- so re-pointing this column at opened_at + casino_settings.round_idle_seconds
-- (default 600) makes that sweep the auto-resolve, with no new timer machinery.
-- `_place_bet`'s `closes_at > ?` guard keeps working unchanged and now reads as
-- "you can't bet into a round that has already been abandoned", which is right.
--
-- INDEX SWAP: one open round per *player* per guild replaces one per channel,
-- mirroring blackjack's "you already have a hand at the table". Safe to apply
-- ahead of the cog switch: the casino is confined to a single configured
-- channel per guild, so guild-scoped and channel-scoped uniqueness describe
-- the same rows, and prod holds zero open rounds in all five tables (rounds
-- resolve within a minute, and the maintenance backstop sweeps stragglers).
--
-- Pools is deliberately NOT touched. It shares RoundTables for the leaver
-- sweep, but it is a day-long communal market with pro-rata payouts and its
-- own opener (`open_pools_round`) — per-player rounds would be meaningless
-- there and pro-rata splits actively wrong.
--
-- Existing rows get user_id = 0. Every one of them is already settled or void
-- (the partial indexes below only see status='open'), so the sentinel is
-- inert: it never collides, and it never gets read.

ALTER TABLE casino_roulette_rounds ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE casino_race_rounds     ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE casino_baccarat_rounds ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE casino_dice_rounds     ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE casino_keno_rounds     ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;

DROP INDEX IF EXISTS idx_casino_roulette_open;
DROP INDEX IF EXISTS idx_casino_race_open;
DROP INDEX IF EXISTS idx_casino_baccarat_open;
DROP INDEX IF EXISTS idx_casino_dice_open;
DROP INDEX IF EXISTS idx_casino_keno_open;

CREATE UNIQUE INDEX idx_casino_roulette_open_player
    ON casino_roulette_rounds (guild_id, user_id) WHERE status = 'open';
CREATE UNIQUE INDEX idx_casino_race_open_player
    ON casino_race_rounds (guild_id, user_id) WHERE status = 'open';
CREATE UNIQUE INDEX idx_casino_baccarat_open_player
    ON casino_baccarat_rounds (guild_id, user_id) WHERE status = 'open';
CREATE UNIQUE INDEX idx_casino_dice_open_player
    ON casino_dice_rounds (guild_id, user_id) WHERE status = 'open';
CREATE UNIQUE INDEX idx_casino_keno_open_player
    ON casino_keno_rounds (guild_id, user_id) WHERE status = 'open';

-- The five per-game betting windows are gone with the communal rounds they
-- paced, replaced by one round_idle_seconds abandonment TTL. Drop any stored
-- values so a guild that tuned them isn't carrying config nothing reads (and
-- so a future setting reusing one of these names can't silently inherit it).
DELETE FROM config WHERE key IN (
    'casino_roulette_window_seconds',
    'casino_derby_window_seconds',
    'casino_baccarat_window_seconds',
    'casino_dice_window_seconds',
    'casino_keno_window_seconds'
);
