-- Member self-service refunds (economy shop): a held streak shield can be
-- refunded in full before it's consumed, but the shield flag alone doesn't
-- record what was paid for it — and the guild's price may have moved since
-- purchase. Snapshot the price paid, mirroring how a rental's `price` column
-- already anchors its own refund math.
ALTER TABLE econ_streaks ADD COLUMN shield_price INTEGER NOT NULL DEFAULT 0;
