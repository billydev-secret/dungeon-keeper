-- Casino War (docs/plans/casino-classics-and-prediction-market.md,
-- Stage 1c): one card each, high card wins even money. 12 of 13 hands
-- settle instantly and never touch this table — a row exists ONLY for the
-- ~1/13 tie awaiting the member's war-or-retreat decision, mirroring the
-- blackjack live-hand pair: one live decision per member (partial unique
-- index), exactly-once settlement via settled_at IS NULL, idle auto-
-- resolve via last_action_at, boot sweep refunds.

CREATE TABLE IF NOT EXISTS casino_war_hands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER NOT NULL DEFAULT 0,
    user_id        INTEGER NOT NULL,
    stake          INTEGER NOT NULL,             -- doubles when war is declared
    state_json     TEXT    NOT NULL,             -- {player, dealer[, war_player, war_dealer]}
    outcome        TEXT,                         -- war_win | war_lose | retreat | refunded
    created_at     REAL    NOT NULL,
    last_action_at REAL    NOT NULL,
    settled_at     REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_war_live
    ON casino_war_hands (guild_id, user_id) WHERE settled_at IS NULL;
