-- The Meadow Derby (docs/plans/casino-derby.md): a fixed-odds critter race
-- joining the casino. Mirrors the roulette pair from 113 exactly — one open
-- race per channel (partial unique index), bets debit at placement through
-- casino_service.take_stake, settlement/void predicates on status='open'
-- so replayed timers and boot sweeps stay exactly-once.

CREATE TABLE IF NOT EXISTS casino_race_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,      -- backfilled after send
    status     TEXT    NOT NULL DEFAULT 'open', -- open | settled | void
    opened_at  REAL    NOT NULL,
    closes_at  REAL    NOT NULL,
    winner     INTEGER,                         -- index into DERBY_FIELD once run
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_race_open
    ON casino_race_rounds (channel_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS casino_race_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    runner     INTEGER NOT NULL,                -- index into DERBY_FIELD
    amount     INTEGER NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,      -- total return, set at settle
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_race_bets_round
    ON casino_race_bets (round_id);
