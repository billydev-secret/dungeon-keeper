-- Migration 138: anti-double-pay anchor for the intake_step income source.
--
-- Intake cards pay the greeter a flat award per checklist step they tick
-- (see bot_modules/economy/intake_rewards.py). The step's own done_at/done_by
-- cannot serve as the dedup key: the manual step button in intake_views is a
-- *toggle*, so unticking clears done_at/done_by and re-ticking would mint the
-- award again, unbounded. This table is the durable anchor instead — one row
-- per (guild, card, step), inserted OR IGNORE inside the same transaction as
-- the tick, so a step pays exactly once for the life of the card no matter
-- how often it is toggled.
--
-- The primary key deliberately excludes user_id: a *different* greeter
-- re-ticking a step someone else already claimed must not mint a second
-- award either. user_id is recorded for attribution/audit only.

CREATE TABLE IF NOT EXISTS econ_intake_rewards (
    guild_id   INTEGER NOT NULL,
    card_id    INTEGER NOT NULL,
    step_key   TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    awarded_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, card_id, step_key)
);

-- "What has this greeter earned from intake lately" — the reporting axis.
CREATE INDEX IF NOT EXISTS idx_econ_intake_rewards_user
    ON econ_intake_rewards (guild_id, user_id, awarded_at);
