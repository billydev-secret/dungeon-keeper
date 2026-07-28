-- Pools — parimutuel prediction market
-- (docs/plans/casino-classics-and-prediction-market.md, Stage 2):
-- one round per guild-local day, opened and resolved by the bot with no
-- admin authoring. Members bet over/under a bot-set line on the day's net
-- change in the economy (mint minus burn). Stakes pool per side; at settle
-- a takeout is BURNED (not fed to the jackpot, which re-mints) and the rest
-- splits pro-rata among the winning side.
--
-- Mirrors the windowed family — stakes debit at placement through
-- casino_service.take_stake, settlement/void predicate on status='open' for
-- exactly-once. Two things differ from roulette/keno/etc:
--   * `line` and `local_day` persist on the round, because the outcome is
--     recomputed from the ledger rather than drawn, so a missed close can
--     settle correctly hours later off the same stored line.
--   * `closes_at` is ~18h after open rather than 45s, so the round sits in
--     status='open' with betting shut for several hours before it settles.
--     Anything sweeping open rounds must respect closes_at, not just status
--     (see the leaver-refund carve-out in pools_service).
--
-- `result` holds the settled metric value as TEXT so it shares RoundTables'
-- result_col shape with the other windowed games.

CREATE TABLE IF NOT EXISTS casino_pools_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,      -- backfilled after send
    status     TEXT    NOT NULL DEFAULT 'open', -- open | settled | void
    local_day  TEXT    NOT NULL,                -- guild-local YYYY-MM-DD measured
    line       REAL    NOT NULL,                -- half-integer: never hit exactly
    opened_at  REAL    NOT NULL,
    closes_at  REAL    NOT NULL,                -- betting shuts; settle is later
    result     TEXT,                            -- settled net change, as TEXT
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_pools_open
    ON casino_pools_rounds (channel_id) WHERE status = 'open';
-- One round per guild per measured day, even across a channel change.
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_pools_guild_day
    ON casino_pools_rounds (guild_id, local_day);

CREATE TABLE IF NOT EXISTS casino_pools_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    side       TEXT    NOT NULL,                -- 'over' | 'under'
    amount     INTEGER NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,      -- total return, set at settle
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_pools_bets_round
    ON casino_pools_bets (round_id);
