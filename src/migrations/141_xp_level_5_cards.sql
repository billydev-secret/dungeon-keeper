-- Where each Level 5 promotion card was posted, so its live "Spicy access"
-- indicator can be re-rendered after the fact (it was rendered once at post
-- time and never revisited). See docs/promotion_review_spec.md.
--
-- Deliberately NOT promotion_review_cards (migration 112): that table's partial
-- unique index idx_promotion_review_open permits a single *unresolved* row per
-- (guild, member), and a Level 5 card never resolves — it would squat that slot
-- and block genuine pruned-return / sleeper cards for the member forever.
CREATE TABLE IF NOT EXISTS xp_level_5_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);

-- The refresh hook's only lookup: every card posted for this member.
CREATE INDEX IF NOT EXISTS idx_xp_level_5_cards_member
    ON xp_level_5_cards (guild_id, user_id);

-- A card *is* its message, so never store one twice — the deferred-promotion
-- recheck loop and the immediate post path can both reach the same send.
CREATE UNIQUE INDEX IF NOT EXISTS idx_xp_level_5_cards_message
    ON xp_level_5_cards (message_id);
