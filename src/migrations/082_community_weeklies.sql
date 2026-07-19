-- Auto-tracking community weeklies (plan: quest-variety stage 3).
--
-- A community quest may now carry a trigger_kind: the same module events
-- that pay personal quests bump a guild-wide counter (NOT board-filtered —
-- every member's action counts), replacing the manager-hand-cranked
-- progress for these quests. Manual (kind-less) community quests keep the
-- old machinery untouched.
--
-- Tiers: 40/70/100% of target (quests.COMMUNITY_TIERS). Each tier crossed
-- pays the quest's flat reward to every 30d-active member, exactly-once via
-- econ_community_tier_payouts reservation rows (tier 0 is reserved for the
-- top-contributor bonus). Contribution counts per member drive that bonus
-- and the "N members contributed" line — cleared at activation, so they are
-- per-run, and the tier-payout rows clear with them (a re-run of the same
-- library quest in a later week must be able to pay again; exactly-once
-- only has to hold within a run, where settlement replays share rows).

CREATE TABLE IF NOT EXISTS econ_community_contrib (
    quest_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (quest_id, user_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS econ_community_tier_payouts (
    quest_id INTEGER NOT NULL,
    tier     INTEGER NOT NULL,   -- 1..3 = milestone tiers, 0 = contributor bonus
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (quest_id, tier, user_id)
) WITHOUT ROWID;

-- Beat-sheet bookkeeping: highest tier already DMed to the host, and
-- whether the final-24h nudge went out (both per-run, reset at activation).
ALTER TABLE econ_community_progress ADD COLUMN notified_tier INTEGER NOT NULL DEFAULT 0;
ALTER TABLE econ_community_progress ADD COLUMN final_notice_sent INTEGER NOT NULL DEFAULT 0;

-- Rotation memory: the ISO week a community quest last ran ('' = never).
ALTER TABLE econ_quests ADD COLUMN last_run_week TEXT NOT NULL DEFAULT '';

-- Gap-week alternation state: the ISO week a community weekly last ran in
-- this guild. NULL = never — the first roll after upgrade activates one.
ALTER TABLE econ_day_marks ADD COLUMN last_community_week TEXT;
