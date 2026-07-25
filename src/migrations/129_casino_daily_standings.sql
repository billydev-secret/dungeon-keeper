-- Casino daily standings: the hub panel's "Today at the tables" line names
-- the day's biggest net winner and biggest net loser. record_play folds each
-- settled play into a per-member, per-guild-local-day (wagered, returned) row
-- in the settlement transaction; net = returned - wagered. Written
-- UNCONDITIONALLY here (unlike casino_daily, which only exists when a wager
-- cap is set), so standings are reliable even for uncapped guilds. Refunds
-- and voids never reach record_play, so a handed-back bet never sways them.

CREATE TABLE IF NOT EXISTS casino_daily_net (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    local_day TEXT    NOT NULL,           -- YYYY-MM-DD, guild-local
    wagered   INTEGER NOT NULL DEFAULT 0,
    returned  INTEGER NOT NULL DEFAULT 0, -- total returned across the day's plays
    PRIMARY KEY (guild_id, user_id, local_day)
);
-- Standings read filters guild + day and ranks by (returned - wagered).
CREATE INDEX IF NOT EXISTS idx_casino_daily_net_day
    ON casino_daily_net (guild_id, local_day);
