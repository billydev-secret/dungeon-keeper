-- Migration 192: daily rotating feature channels (2026-08-29).
--
-- One room out of a configured pool is visible each day; the rest are hidden
-- by denying view_channel to @everyone IN PLACE. This deliberately does NOT
-- reuse the `hidden_channels` table or its cog path: that one moves a channel
-- into a "Hidden Channels" category and restores its position with a second
-- edit, which is right for an indefinite hide and wrong for a daily one (it
-- reshuffles every member's channel list twice a day). Only the pure
-- serialisation helpers in bot_modules/hidden_channels/overwrites.py are
-- shared. `stored_overwrites` here is that same JSON shape.
--
-- Two clocks, deliberately separate:
--   * "local" is the guild's shared `tz_offset_hours` config key, the same one
--     birthdays, jail, reports and the quest board already read. This table
--     deliberately does NOT carry its own offset: two dials could be set to
--     different values, and the flip would then fire hours away from the day
--     boundary the board froze its pool on -- exactly the desync the next
--     note says is impossible.
--   * the FLIP is locked to local midnight, because the quest board's period
--     is date.toordinal(local_day) and its pool is frozen at the first read
--     after midnight. A flip at any other hour would freeze a pool describing
--     yesterday's featured room and leave the board and the open room
--     disagreeing until the flip landed.
--   * the ANNOUNCEMENT is configurable (default 09:00), so the post lands when
--     main chat is awake. It still tells the truth: the room changed at 00:00.
-- `last_flip_date` / `last_announce_date` are the exactly-once guards, claimed
-- with a conditional UPDATE the way announcements_service.claim_scheduled does.
--
-- There is no stored cursor. Which room is featured is DERIVED from the day
-- ordinal, exactly as quests.assigned_quest_ids derives a board from
-- period_index: start = (ordinal * rooms_per_day) % len(pool). The room and
-- the board therefore move in lockstep by construction, and a bot that was
-- down for three days comes back on the correct room rather than three
-- behind.
--
-- No per-user data: neither table names a member, so no data_register.md row.
--
-- `pause_when_off` is deliberately absent. Pausing new submissions while a
-- room is hidden is stage 3; CLAUDE.md forbids shipping a toggle that isn't
-- enforced, so the column arrives with the enforcement.

CREATE TABLE IF NOT EXISTS feature_rotation_config (
    guild_id            INTEGER PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 0,
    announce_channel_id INTEGER NOT NULL DEFAULT 0,
    announce_hour       INTEGER NOT NULL DEFAULT 9,
    rooms_per_day       INTEGER NOT NULL DEFAULT 1,
    last_flip_date      TEXT    NOT NULL DEFAULT '',
    last_announce_date  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS feature_rotation_pool (
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    position          INTEGER NOT NULL DEFAULT 0,
    label             TEXT    NOT NULL DEFAULT '',
    blurb             TEXT    NOT NULL DEFAULT '',
    in_rotation       INTEGER NOT NULL DEFAULT 1,
    hide_when_off     INTEGER NOT NULL DEFAULT 1,
    announce          INTEGER NOT NULL DEFAULT 1,
    -- Comma-separated econ_quests.trigger_kind values. `quest_kinds` are the
    -- quests this room owns (the featured pin draws from them); the
    -- `blocked_kinds` subset cannot be completed while the room is hidden
    -- (their entry point is a button on an in-channel message), so those are
    -- held out of the board pool on hidden days. A kind absent from
    -- blocked_kinds keeps working hidden — /confess, /guess submit and the
    -- whisper panel are all reachable with the room shut.
    quest_kinds       TEXT    NOT NULL DEFAULT '',
    blocked_kinds     TEXT    NOT NULL DEFAULT '',
    stored_overwrites TEXT    NOT NULL DEFAULT '',
    hidden_at         REAL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_feature_rotation_pool_order
    ON feature_rotation_pool(guild_id, position, channel_id);
