-- Economy quests — XP rewards and the onboarding path.
--
-- `reward_xp` pays levelling XP alongside the coin reward on every quest
-- payout (instant and approved sign-off both flow through _credit_reward).
-- XP takes no booster multiplier — the ×1.5 is a currency-faucet patron
-- bonus, and minting XP would distort the level curve.
--
-- `onboarding` flags quests that make up the new-member path: on join the
-- bot DMs the member the guild's active onboarding quests, once per member
-- ever (`econ_onboarding_dms` dedupes rejoins).

ALTER TABLE econ_quests ADD COLUMN reward_xp INTEGER NOT NULL DEFAULT 0;
ALTER TABLE econ_quests ADD COLUMN onboarding INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS econ_onboarding_dms (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    sent_at   REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
