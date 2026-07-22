-- Tracks promotion-review cards posted for members who lost access and came
-- back. Two triggers write here (see promotion_review_service):
--   * pruned_return — an auto sweep pulled a role (role_prune_events, mig 098)
--     and the member posted again;
--   * sleeper       — an inactive-held member (inactive_members, mig 057)
--     posted in the sleeper channel.
-- The open row (resolved_at IS NULL) dedupes so a member gets a single card no
-- matter how many messages they send; resolving it records who granted access
-- / reactivated / dismissed and closes the loop.
CREATE TABLE IF NOT EXISTS promotion_review_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'pruned_return',
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by INTEGER,
    resolution TEXT
);

-- One open card per (guild, member): the dedup gate the message hot path relies on.
CREATE UNIQUE INDEX IF NOT EXISTS idx_promotion_review_open
    ON promotion_review_cards (guild_id, user_id)
    WHERE resolved_at IS NULL;
