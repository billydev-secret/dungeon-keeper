-- Live auctions (plan: docs/plans/economy-auctions.md).
--
-- A mod opens a freeform, mod-fulfilled auction; members bid up in the open; the
-- winning bid is destroyed (the sink). The bounty's sibling — escrow via
-- apply_debit, refunds via apply_credit — but instead of many contributions
-- pooling, exactly ONE bid is live at a time and the previous high bidder is
-- refunded the instant they're outbid.
--
-- Money: a bid is an `apply_debit` (auction_bid) into escrow, so you can never
-- bid money you don't have and the winner is already charged before close.
-- Being outbid credits the loser back in full (auction_refund). At close the
-- winning bid is simply never refunded → burned; a mod-curated prize is granted
-- out of band, so nothing flows back in. Cancel refunds the standing high bid.
--
-- The standing high bid lives on the auction row (one read, atomic conditional
-- update guards the two-bids race). The bids table is the audit trail + refund
-- ledger: exactly one row is 'escrowed'/'won' at a time, the rest 'refunded'.
--
-- One live auction per guild for v1 (open_auction rejects while another is open).

CREATE TABLE IF NOT EXISTS econ_auctions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    channel_id         INTEGER NOT NULL DEFAULT 0,
    message_id         INTEGER NOT NULL DEFAULT 0,
    title              TEXT    NOT NULL,
    description        TEXT    NOT NULL DEFAULT '',
    created_by         INTEGER NOT NULL,
    state              TEXT    NOT NULL DEFAULT 'open'
                               CHECK (state IN ('open','closed','cancelled')),
    min_bid            INTEGER NOT NULL,
    min_increment      INTEGER NOT NULL,
    soft_close_seconds INTEGER NOT NULL,
    ends_at            REAL    NOT NULL,
    high_bid           INTEGER,          -- current standing bid (NULL until first)
    high_bidder_id     INTEGER,
    winner_id          INTEGER,          -- frozen at close (= high_bidder_id)
    winning_bid        INTEGER NOT NULL DEFAULT 0,   -- the burned amount
    resolver_id        INTEGER,          -- the mod who cancelled / force-closed
    created_at         REAL    NOT NULL,
    closed_at          REAL
);

CREATE TABLE IF NOT EXISTS econ_auction_bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id  INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'escrowed'
                        CHECK (state IN ('escrowed','refunded','won')),
    created_at  REAL    NOT NULL,
    refunded_at REAL
);

-- At most one open auction per guild (the single-live-auction rule) and the
-- settle sweep over auctions past their end.
CREATE INDEX IF NOT EXISTS idx_econ_auctions_state
    ON econ_auctions (guild_id, state, ends_at);

-- Every bid on an auction (history count + the escrowed/refunded ledger).
CREATE INDEX IF NOT EXISTS idx_econ_auction_bids
    ON econ_auction_bids (auction_id, state);
