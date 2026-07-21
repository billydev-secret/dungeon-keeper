-- Migration 099: idempotency ledger for external-game economy payouts.
--
-- A parsed external game (e.g. a Gamebot CAH "Game over!") pays participation +
-- a win bonus through pay_game_rewards. That faucet is NOT occurrence-deduped,
-- so the payout must fire exactly once per game. games_external_messages'
-- parse_status is reset whenever a message is re-captured on edit (Gamebot
-- posts "Loading…" then edits in the real embed), so it can't be the dedup
-- authority. This tiny ledger is: one row per paid game, keyed on the terminal
-- message id, written before the payout. INSERT OR IGNORE + rowcount tells the
-- caller whether it's the first to pay.

CREATE TABLE IF NOT EXISTS games_external_payouts (
    message_id  INTEGER PRIMARY KEY,          -- the terminal (e.g. Game over!) msg id
    guild_id    INTEGER NOT NULL,
    kind        TEXT    NOT NULL,             -- watch kind, e.g. 'gamebot_cah'
    paid_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_games_ext_payouts_guild
    ON games_external_payouts (guild_id);
