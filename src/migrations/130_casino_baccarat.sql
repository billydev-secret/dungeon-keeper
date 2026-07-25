-- Baccarat (docs/plans/casino-classics-and-prediction-market.md, Stage 1a):
-- windowed punto-banco joining the casino. Mirrors the roulette/derby pair —
-- one open coup per channel (partial unique index), bets debit at placement
-- through casino_service.take_stake, settlement/void predicates on
-- status='open' so replayed timers and boot sweeps stay exactly-once. The
-- dealt coup persists as a JSON card list in `result` (both hands), since
-- unlike roulette's single number the outcome is the cards themselves.

CREATE TABLE IF NOT EXISTS casino_baccarat_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,      -- backfilled after send
    status     TEXT    NOT NULL DEFAULT 'open', -- open | settled | void
    opened_at  REAL    NOT NULL,
    closes_at  REAL    NOT NULL,
    result     TEXT,                            -- JSON {player:[..], banker:[..]}
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_baccarat_open
    ON casino_baccarat_rounds (channel_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS casino_baccarat_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    side       TEXT    NOT NULL,                -- player | banker | tie
    amount     INTEGER NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,      -- total return, set at settle
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_baccarat_bets_round
    ON casino_baccarat_bets (round_id);
