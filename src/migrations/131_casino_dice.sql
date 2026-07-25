-- Dice / Sic Bo (docs/plans/casino-classics-and-prediction-market.md,
-- Stage 1b): three dice, one communal roll. Mirrors the roulette/derby/
-- baccarat windowed pair — one open roll per channel (partial unique
-- index), bets debit at placement through casino_service.take_stake,
-- settlement/void predicates on status='open' so replayed timers and boot
-- sweeps stay exactly-once. The roll persists as JSON [d1, d2, d3] in
-- `result` (three dice, not a single number).

CREATE TABLE IF NOT EXISTS casino_dice_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,      -- backfilled after send
    status     TEXT    NOT NULL DEFAULT 'open', -- open | settled | void
    opened_at  REAL    NOT NULL,
    closes_at  REAL    NOT NULL,
    result     TEXT,                            -- JSON [d1, d2, d3]
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_dice_open
    ON casino_dice_rounds (channel_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS casino_dice_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    bet_type   TEXT    NOT NULL,                -- big | small | odd | even
    amount     INTEGER NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,      -- total return, set at settle
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_dice_bets_round
    ON casino_dice_bets (round_id);
