-- The Golden Meadow casino (docs/plans/casino.md): house gambling games
-- (coinflip, slots, blackjack, roulette) staking the guild currency in one
-- configured casino channel. Settings live as casino_* keys in the shared
-- config KV table (economy pattern) — nothing here but game state.
--
-- Money discipline mirrors econ_game_wagers (094): every stake debits
-- through casino_service.take_stake, every settlement/refund predicates on
-- settled_at IS NULL so replayed timers and boot sweeps are exactly-once.

-- Per-member daily wager accounting for the configurable cap. The counter
-- is bumped inside the same transaction as the stake's debit (the
-- econ_logins INSERT OR IGNORE family, with an amount instead of a flag).
CREATE TABLE IF NOT EXISTS casino_daily (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    local_day TEXT    NOT NULL,   -- guild-local YYYY-MM-DD
    wagered   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, local_day)
);

-- One live blackjack hand per member (partial unique index). state_json
-- carries {deck, player, dealer} so a hand could in principle resume; the
-- shipped posture is simpler — boot refunds every live hand (honest reset),
-- and the idle sweep auto-stands hands whose player walked away.
CREATE TABLE IF NOT EXISTS casino_blackjack_hands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER NOT NULL DEFAULT 0,  -- backfilled after send
    user_id        INTEGER NOT NULL,
    stake          INTEGER NOT NULL,            -- total staked; doubles fold in
    doubled        INTEGER NOT NULL DEFAULT 0,
    state_json     TEXT    NOT NULL,
    outcome        TEXT,                        -- blackjack|win|push|lose|bust|refunded
    created_at     REAL    NOT NULL,
    last_action_at REAL    NOT NULL,
    settled_at     REAL                         -- exactly-once guard
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_bj_live
    ON casino_blackjack_hands (guild_id, user_id) WHERE settled_at IS NULL;

-- Roulette: one open betting round per channel; bets debit at placement and
-- settle (or refund, on void) when the wheel spins at closes_at.
CREATE TABLE IF NOT EXISTS casino_roulette_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,      -- backfilled after send
    status     TEXT    NOT NULL DEFAULT 'open', -- open | settled | void
    opened_at  REAL    NOT NULL,
    closes_at  REAL    NOT NULL,
    result     INTEGER,                         -- 0-36 once spun
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_roulette_open
    ON casino_roulette_rounds (channel_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS casino_roulette_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    bet_type   TEXT    NOT NULL,                -- red | black | dozen | number
    selection  INTEGER NOT NULL DEFAULT 0,      -- dozen 1-3 / number 0-36
    amount     INTEGER NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,      -- total return, set at settle
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_roulette_bets_round
    ON casino_roulette_bets (round_id);
