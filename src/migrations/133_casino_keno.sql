-- Keno (docs/plans/casino-classics-and-prediction-market.md, Stage 1d):
-- 20 of 80 numbers drawn once per communal round; tickets are quick-picked
-- 4/6/8/10-spot sets paying by catch count off a bespoke ~95%-RTP paytable
-- (real casino keno's 65-75% has no place here). Mirrors the windowed
-- family — one open draw per channel (partial unique index), tickets debit
-- at placement through casino_service.take_stake, settlement/void
-- predicates on status='open'. The draw persists as JSON in `result`; each
-- ticket's numbers persist as JSON in `spots`.

CREATE TABLE IF NOT EXISTS casino_keno_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,      -- backfilled after send
    status     TEXT    NOT NULL DEFAULT 'open', -- open | settled | void
    opened_at  REAL    NOT NULL,
    closes_at  REAL    NOT NULL,
    result     TEXT,                            -- JSON [20 drawn numbers]
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_keno_open
    ON casino_keno_rounds (channel_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS casino_keno_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    spots      TEXT    NOT NULL,                -- JSON [ticket numbers]
    amount     INTEGER NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,      -- total return, set at settle
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_keno_bets_round
    ON casino_keno_bets (round_id);
