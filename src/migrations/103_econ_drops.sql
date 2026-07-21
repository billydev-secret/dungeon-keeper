-- Coin Drops: the bot drops a pouch of coins in a configured channel at
-- random intervals; the first member to press the drop message's Claim
-- button collects it. One row per posted drop; claim is a conditional
-- UPDATE on status='open' (first winner takes it, losers see rowcount 0).
-- The row is created before the message is sent (the button's custom_id
-- carries the drop id), so message_id starts 0 and is backfilled.
CREATE TABLE IF NOT EXISTS econ_drops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open | claimed | expired
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    claimed_by INTEGER,
    claimed_at REAL
);

-- The loop sweeps open drops (expiry + the one-open-pouch-per-guild gate).
CREATE INDEX IF NOT EXISTS idx_econ_drops_status
    ON econ_drops (status, expires_at);
