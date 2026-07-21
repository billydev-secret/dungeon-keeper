-- Once-per-guild-local-day dedup anchor for the Photo Challenge flat
-- participation award (EconSettings.reward_photo_post).
--
-- Mirrors econ_logins / econ_qotd_rewards: an INSERT OR IGNORE riding the
-- credit's transaction pays the flat award at most once per member per local
-- day, no matter how many photos they post. The photo_post *quest* bonus
-- dedups separately through econ_quest_claims; this table only guards the flat
-- faucet, so the two payouts are independent (both capped once/day).

CREATE TABLE IF NOT EXISTS econ_photo_rewards (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    local_day TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, local_day)
);
