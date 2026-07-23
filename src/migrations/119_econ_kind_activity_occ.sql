-- Distinct-occurrence log for ENTITY-keyed trigger kinds.
--
-- econ_kind_activity counts RAW fires per (guild, member, kind, day). That is
-- correct for message-id-keyed kinds (message_sent, reply_sent, …), which are
-- 1:1 with their occurrence, and for community volume goals. But for kinds
-- whose occurrence is an ENTITY that recurs within a period — a voice partner,
-- a channel, a day, a message author, a newcomer, a thread — the raw count is
-- NOT what a counted quest measures: "voice with N different people" advances
-- only on a new partner, so sizing the target off the undeduped stream pinned
-- a member to a mathematically unreachable ceiling (talk to one partner 120×,
-- get target 10, never pass 1).
--
-- This table logs each distinct (kind, day, occurrence) for those entity-keyed
-- kinds so resolve_member_target's trailing-period sizing can COUNT(DISTINCT
-- occurrence) — the same quantity _bump_progress advances — instead of the raw
-- SUM(count). Written alongside econ_kind_activity by record_kind_activity;
-- pruned on the same day-roll hygiene pass.
CREATE TABLE IF NOT EXISTS econ_kind_activity_occ (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    local_day  TEXT    NOT NULL,   -- guild-local "YYYY-MM-DD"
    occurrence TEXT    NOT NULL,   -- the entity id (partner/channel/day/…)
    PRIMARY KEY (guild_id, user_id, kind, local_day, occurrence)
) WITHOUT ROWID;

-- Trailing sizing counts distinct occurrences over a (guild, member, kind)
-- day range.
CREATE INDEX IF NOT EXISTS idx_econ_kind_activity_occ_lookup
    ON econ_kind_activity_occ (guild_id, user_id, kind, local_day);
