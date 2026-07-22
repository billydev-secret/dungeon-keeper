-- Community Bounty (plan: docs/plans/community-bounty.md).
--
-- Anyone posts a freeform task and seeds a pot; anyone chips in; a mod awards
-- the pot to the winner minus a house rake that evaporates. If nobody's awarded
-- it, every contributor is refunded. The economy's first many-payer mechanic —
-- so contributions are their own rows, keyed by bounty, for exact per-member
-- refunds (exactly-once via refunded_at, the same guard the pin/sponsor use).
--
-- Money: each contribution is an `apply_debit` (bounty_stake) into escrow. Award
-- credits ONE payout (bounty_payout) to the winner; the rake is the escrowed
-- coins never credited back — the burn. Cancel/expire credit each contribution
-- back (bounty_refund), never raked.
--
-- Pot is never stored: it's SUM(contributions WHERE refunded_at IS NULL), so it
-- can't drift from the ledger.

CREATE TABLE IF NOT EXISTS econ_bounties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    poster_id       INTEGER NOT NULL,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    state           TEXT    NOT NULL DEFAULT 'open'
                            CHECK (state IN ('open','awarded','cancelled','expired')),
    winner_id       INTEGER,          -- set on award
    payout          INTEGER NOT NULL DEFAULT 0,   -- what the winner got (pot - rake)
    rake_amount     INTEGER NOT NULL DEFAULT 0,   -- what evaporated on award
    card_channel_id INTEGER NOT NULL DEFAULT 0,
    card_message_id INTEGER NOT NULL DEFAULT 0,
    resolver_id     INTEGER,          -- the mod who awarded/cancelled
    created_at      REAL    NOT NULL,
    resolved_at     REAL,
    expires_at      REAL
);

CREATE TABLE IF NOT EXISTS econ_bounty_contributions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bounty_id   INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    refunded_at REAL,
    created_at  REAL    NOT NULL
);

-- The board (open bounties, oldest-first) and the expiry sweep.
CREATE INDEX IF NOT EXISTS idx_econ_bounties_state
    ON econ_bounties (guild_id, state, created_at);

-- Every contribution to a bounty (pot sum + per-member refunds).
CREATE INDEX IF NOT EXISTS idx_econ_bounty_contrib
    ON econ_bounty_contributions (bounty_id);
