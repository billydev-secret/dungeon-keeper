-- 030_rules_watch.sql
-- Passive all-channel moderation monitor: event log and human label capture.

CREATE TABLE IF NOT EXISTS rules_events (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id                      INTEGER NOT NULL,
    message_id                    INTEGER NOT NULL,
    author_id                     INTEGER NOT NULL,
    target_id                     INTEGER,
    target_confidence             TEXT,
    channel_id                    INTEGER NOT NULL,
    window_json                   TEXT,
    -- Content signals
    guard_verdict                 TEXT,
    guard_rule                    TEXT,
    guard_reason                  TEXT,
    guard_confidence              REAL,
    slur_signal                   INTEGER NOT NULL DEFAULT 0,
    vader_compound                REAL,
    vader_trajectory              REAL,
    -- Context signals
    mutual_interaction_count      INTEGER,
    reciprocity_ratio             REAL,
    consent_pair_active           INTEGER NOT NULL DEFAULT 0,
    consent_pair_recently_revoked INTEGER NOT NULL DEFAULT 0,
    dm_tier_mismatch              INTEGER NOT NULL DEFAULT 0,
    thread_reciprocity_ratio      REAL,
    persistence_count             INTEGER NOT NULL DEFAULT 0,
    boundary_token_crossed        INTEGER NOT NULL DEFAULT 0,
    target_withdrew               INTEGER NOT NULL DEFAULT 0,
    tenure_days                   INTEGER,
    -- Scoring
    priority_score                REAL,
    priority_tier                 TEXT,
    priority_reason               TEXT,
    -- State
    alert_message_id              INTEGER,
    detected_at                   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_events_guild_ts
    ON rules_events (guild_id, detected_at);

CREATE INDEX IF NOT EXISTS idx_rules_events_tier
    ON rules_events (guild_id, priority_tier);

CREATE TABLE IF NOT EXISTS rules_labels (
    event_id        INTEGER PRIMARY KEY REFERENCES rules_events (id),
    is_violation    INTEGER,
    corrected_rule  TEXT,
    labeled_by      INTEGER,
    labeled_at      REAL,
    notes           TEXT
);
