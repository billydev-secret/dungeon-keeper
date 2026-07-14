-- Chat Revive ("Ember") — lull detection + curated question bank.
--
-- `revive_guild_config` / `revive_channel_config` hold the dials from the
-- spec's protections table; a channel row existing with enabled=1 is the
-- "admin explicitly invited the bot here" gate. `categories` is a JSON array
-- of bank categories this channel draws from (empty = all SFW categories).
--
-- `revive_questions` is the guild-owned bank (econ_quests shape + the
-- legitlibs use_count/last_used_at stat columns). NSFW questions only ever
-- surface where Discord's channel age-restriction flag allows them.
--
-- `revive_events` is the single source of truth for every frequency gate:
-- daily budget (COUNT per guild local_day), guild breathing room and channel
-- rest (MAX created_at), ping scarcity (MAX created_at WHERE pinged). Rows are
-- written only after a successful send. `measured_at`/`success` are filled by
-- the loop's 30-minute follow-up sweep.
--
-- `revive_channel_rhythm` caches the learned per-band gap statistics so the
-- monitor tick and the /revive check preview read the same numbers; it is
-- recomputed from processed_messages when stale.
--
-- The processed_messages index is new: the table only had a user-leading
-- index, and rhythm learning + silence checks need channel-scoped time scans.

CREATE TABLE IF NOT EXISTS revive_guild_config (
    guild_id           INTEGER PRIMARY KEY,
    enabled            INTEGER NOT NULL DEFAULT 0,
    role_id            INTEGER,
    quiet_start        INTEGER NOT NULL DEFAULT 0,
    quiet_end          INTEGER NOT NULL DEFAULT 8,
    daily_budget       INTEGER NOT NULL DEFAULT 3,
    guild_gap_minutes  INTEGER NOT NULL DEFAULT 90,
    flourish_enabled   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS revive_channel_config (
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    categories       TEXT    NOT NULL DEFAULT '[]',
    ping_enabled     INTEGER NOT NULL DEFAULT 0,
    role_id_override INTEGER,
    rest_hours       REAL    NOT NULL DEFAULT 8.0,
    fire_multiplier  REAL    NOT NULL DEFAULT 4.0,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS revive_questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'general',
    nsfw         INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER,
    created_at   REAL    NOT NULL,
    use_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL
);

CREATE INDEX IF NOT EXISTS idx_revive_questions_guild
    ON revive_questions (guild_id, active, category);

CREATE TABLE IF NOT EXISTS revive_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    question_id    INTEGER,
    message_id     INTEGER,
    trigger_kind   TEXT    NOT NULL DEFAULT 'auto' CHECK (trigger_kind IN ('auto', 'manual')),
    pinged         INTEGER NOT NULL DEFAULT 0,
    local_day      TEXT    NOT NULL,
    created_at     REAL    NOT NULL,
    measured_at    REAL,
    follow_msgs    INTEGER,
    follow_authors INTEGER,
    success        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_revive_events_guild_ts
    ON revive_events (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_revive_events_channel_ts
    ON revive_events (guild_id, channel_id, created_at);

CREATE TABLE IF NOT EXISTS revive_channel_rhythm (
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    band         INTEGER NOT NULL,
    median_gap   REAL    NOT NULL,
    p90_gap      REAL    NOT NULL,
    msgs_per_day REAL    NOT NULL,
    gap_count    INTEGER NOT NULL,
    computed_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, channel_id, band)
);

CREATE INDEX IF NOT EXISTS idx_pm_channel_ts
    ON processed_messages (guild_id, channel_id, created_at);
