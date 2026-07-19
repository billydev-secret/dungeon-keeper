-- Per-member trigger-kind activity ledger (plan: quest-variety stage 1).
--
-- One row per (guild, member, kind, guild-local day), incremented by
-- fire_trigger_quests on EVERY occurrence — before the personal-board
-- filter and the income-source switch — so it measures what members
-- actually do, not just what happened to pay. It feeds dynamic target
-- sizing on both surfaces: a member's counted-quest target derives from
-- their own trailing periods (economy/quests.effective_target's dynamic
-- path), and a community weekly's target from the guild-wide sums.
--
-- Rows older than ~10 trailing weeks are pruned on the economy loop's
-- day roll; the table stays small (active members x active kinds x 70).
-- Historical warm-up for the xp_events-mirrored kinds (message_sent,
-- reply_sent, reaction_given, voice_session, qotd_reply) is done by the
-- stage-2 seed script in Python — local-day bucketing needs per-guild tz
-- offsets, which plain SQL can't resolve.

CREATE TABLE IF NOT EXISTS econ_kind_activity (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    local_day TEXT    NOT NULL,   -- guild-local "YYYY-MM-DD"
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, kind, local_day)
) WITHOUT ROWID;

-- Community auto-sizing sums a kind across all members by day.
CREATE INDEX IF NOT EXISTS idx_econ_kind_activity_guild_kind_day
    ON econ_kind_activity (guild_id, kind, local_day);
