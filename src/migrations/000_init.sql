-- Migration 000: Initial schema baseline
-- Captures all tables created by init_*_tables() functions as of project start.
-- Future schema changes go in 001_*.sql, 002_*.sql, etc.

-- ── db_utils.py ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS config (
    guild_id INTEGER NOT NULL DEFAULT 0,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS config_ids (
    guild_id INTEGER NOT NULL DEFAULT 0,
    bucket TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (guild_id, bucket, value)
);

CREATE TABLE IF NOT EXISTS grant_roles (
    guild_id            INTEGER NOT NULL,
    grant_name          TEXT NOT NULL,
    label               TEXT NOT NULL DEFAULT '',
    role_id             INTEGER NOT NULL DEFAULT 0,
    log_channel_id      INTEGER NOT NULL DEFAULT 0,
    announce_channel_id INTEGER NOT NULL DEFAULT 0,
    grant_message       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, grant_name)
);

CREATE TABLE IF NOT EXISTS grant_role_permissions (
    guild_id    INTEGER NOT NULL,
    grant_name  TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('user', 'role')),
    entity_id   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, grant_name, entity_type, entity_id)
);

-- ── xp_system.py ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS member_xp (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    total_xp REAL NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 1,
    last_message_at REAL,
    last_message_norm TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS voice_sessions (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    session_started_at REAL NOT NULL,
    qualified_since REAL,
    awarded_intervals INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS xp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    amount REAL NOT NULL,
    created_at REAL NOT NULL,
    channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS processed_messages (
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    processed_at REAL NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);

CREATE TABLE IF NOT EXISTS member_activity (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    last_channel_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    last_message_at REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS role_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_name TEXT NOT NULL,
    action TEXT NOT NULL,
    granted_at REAL NOT NULL
);

-- ── services/auto_delete_service.py ───────────────────────────────────

CREATE TABLE IF NOT EXISTS auto_delete_rules (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    max_age_seconds INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    last_run_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS auto_delete_messages (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (guild_id, channel_id, message_id)
);

-- ── services/message_store.py ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS messages (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    content     TEXT,
    reply_to_id INTEGER,
    ts          INTEGER NOT NULL,
    sentiment   REAL,
    emotion     TEXT
);

CREATE TABLE IF NOT EXISTS message_attachments (
    message_id  INTEGER NOT NULL,
    url         TEXT NOT NULL,
    PRIMARY KEY (message_id, url)
);

CREATE TABLE IF NOT EXISTS message_mentions (
    message_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS message_reactions (
    message_id  INTEGER NOT NULL,
    emoji       TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, emoji)
);

CREATE TABLE IF NOT EXISTS message_embeds (
    message_id  INTEGER NOT NULL,
    embed_index INTEGER NOT NULL,
    title       TEXT,
    description TEXT,
    url         TEXT,
    author_name TEXT,
    footer_text TEXT,
    fields_json TEXT,
    PRIMARY KEY (message_id, embed_index)
);

CREATE TABLE IF NOT EXISTS reaction_log (
    guild_id    INTEGER NOT NULL,
    reactor_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id, reactor_id)
);

CREATE TABLE IF NOT EXISTS known_users (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    display_name    TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL DEFAULT 0,
    is_bot          INTEGER NOT NULL DEFAULT 0,
    current_member  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS member_events (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    ts          REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, event_type, ts)
);

CREATE TABLE IF NOT EXISTS known_channels (
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    channel_name    TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id)
);

-- ── services/moderation.py ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS jails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    moderator_id    INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    stored_roles    TEXT NOT NULL DEFAULT '[]',
    channel_id      INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    expires_at      REAL,
    released_at     REAL,
    release_reason  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL DEFAULT 0,
    description     TEXT NOT NULL DEFAULT '',
    source_message_url TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    claimer_id      INTEGER,
    escalated       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    closed_at       REAL,
    closed_by       INTEGER,
    close_reason    TEXT NOT NULL DEFAULT '',
    deleted_at      REAL
);

CREATE TABLE IF NOT EXISTS ticket_participants (
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    user_id     INTEGER NOT NULL,
    added_by    INTEGER NOT NULL,
    added_at    REAL NOT NULL,
    removed_at  REAL,
    PRIMARY KEY (ticket_id, user_id)
);

CREATE TABLE IF NOT EXISTS warnings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    moderator_id    INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    revoked         INTEGER NOT NULL DEFAULT 0,
    revoked_at      REAL,
    revoked_by      INTEGER,
    revoke_reason   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    actor_id    INTEGER NOT NULL,
    target_id   INTEGER,
    extra       TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    record_id   INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    creator_id      INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    vote_text       TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    vote_started_at REAL,
    vote_ended_at   REAL
);

CREATE TABLE IF NOT EXISTS policy_votes (
    policy_id   INTEGER NOT NULL REFERENCES policy_tickets(id),
    user_id     INTEGER NOT NULL,
    vote        TEXT NOT NULL,
    voted_at    REAL NOT NULL,
    PRIMARY KEY (policy_id, user_id)
);

CREATE TABLE IF NOT EXISTS policies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    policy_ticket_id  INTEGER NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    passed_at         REAL NOT NULL
);

-- ── services/interaction_graph.py ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_interactions (
    guild_id     INTEGER NOT NULL,
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    weight       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, from_user_id, to_user_id)
);

CREATE TABLE IF NOT EXISTS user_interactions_log (
    guild_id     INTEGER NOT NULL,
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    message_id   INTEGER
);

-- ── services/invite_tracker.py ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS invite_edges (
    guild_id    INTEGER NOT NULL,
    inviter_id  INTEGER NOT NULL,
    invitee_id  INTEGER NOT NULL,
    joined_at   REAL NOT NULL,
    invite_code TEXT,
    PRIMARY KEY (guild_id, invitee_id)
);

-- ── services/inactivity_prune_service.py ──────────────────────────────

CREATE TABLE IF NOT EXISTS inactivity_prune_rules (
    guild_id        INTEGER PRIMARY KEY,
    role_id         INTEGER NOT NULL,
    inactivity_days INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inactivity_prune_exceptions (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- ── services/gender_service.py ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS member_gender (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    gender    TEXT NOT NULL,
    set_by    INTEGER NOT NULL,
    set_at    REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- ── services/health_service.py ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS health_metrics_cache (
    guild_id     INTEGER NOT NULL,
    metric_key   TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    computed_at  REAL    NOT NULL,
    ttl_seconds  INTEGER NOT NULL DEFAULT 900,
    PRIMARY KEY (guild_id, metric_key)
);

CREATE TABLE IF NOT EXISTS message_sentiment (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    sentiment   REAL    NOT NULL,
    emotion     TEXT,
    computed_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    event_type   TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    channel_id   INTEGER,
    details_json TEXT    NOT NULL DEFAULT '{}',
    detected_at  REAL    NOT NULL,
    resolved_at  REAL,
    resolved_by  INTEGER
);

CREATE TABLE IF NOT EXISTS message_velocity_baseline (
    guild_id    INTEGER NOT NULL,
    hour_of_day INTEGER NOT NULL,
    day_of_week INTEGER NOT NULL,
    mean_rate   REAL    NOT NULL,
    stddev_rate REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, hour_of_day, day_of_week)
);

-- ── services/member_quality_score.py ─────────────────────────────────

CREATE TABLE IF NOT EXISTS quality_score_leaves (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    start_ts    REAL NOT NULL,
    end_ts      REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- ── services/booster_roles.py ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS booster_roles (
    guild_id   INTEGER NOT NULL,
    role_key   TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    role_id    INTEGER NOT NULL DEFAULT 0,
    image_path TEXT    NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, role_key)
);

CREATE TABLE IF NOT EXISTS booster_panel_messages (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);

-- ── commands/watch_commands.py ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS watched_users (
    guild_id        INTEGER NOT NULL,
    watched_user_id INTEGER NOT NULL,
    watcher_user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, watched_user_id, watcher_user_id)
);

-- ── services/wellness_service.py ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS wellness_config (
    guild_id               INTEGER PRIMARY KEY,
    role_id                INTEGER NOT NULL DEFAULT 0,
    channel_id             INTEGER NOT NULL DEFAULT 0,
    active_list_message_id INTEGER NOT NULL DEFAULT 0,
    crisis_resource_url    TEXT NOT NULL DEFAULT '',
    default_enforcement    TEXT NOT NULL DEFAULT 'gradual'
);

CREATE TABLE IF NOT EXISTS wellness_users (
    guild_id               INTEGER NOT NULL,
    user_id                INTEGER NOT NULL,
    timezone               TEXT NOT NULL DEFAULT 'UTC',
    enforcement_level      TEXT NOT NULL DEFAULT 'gradual',
    notifications_pref     TEXT NOT NULL DEFAULT 'both',
    slow_mode_rate_seconds INTEGER NOT NULL DEFAULT 120,
    public_commitment      INTEGER NOT NULL DEFAULT 1,
    away_enabled           INTEGER NOT NULL DEFAULT 0,
    away_message           TEXT NOT NULL DEFAULT '',
    daily_reset_hour       INTEGER NOT NULL DEFAULT 0,
    opted_in_at            REAL,
    opted_out_at           REAL,
    paused_until           REAL,
    cooldown_until         REAL,
    last_nudge_at          REAL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS wellness_caps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    label           TEXT NOT NULL,
    scope           TEXT NOT NULL,
    scope_target_id INTEGER NOT NULL DEFAULT 0,
    window          TEXT NOT NULL,
    cap_limit       INTEGER NOT NULL,
    exclude_exempt  INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    bucket_limits   TEXT
);

CREATE TABLE IF NOT EXISTS wellness_cap_counters (
    cap_id             INTEGER NOT NULL,
    window_start_epoch INTEGER NOT NULL,
    count              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cap_id, window_start_epoch)
);

CREATE TABLE IF NOT EXISTS wellness_cap_overages (
    cap_id             INTEGER NOT NULL,
    window_start_epoch INTEGER NOT NULL,
    overage_count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cap_id, window_start_epoch)
);

CREATE TABLE IF NOT EXISTS wellness_blackouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL,
    days_mask    INTEGER NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wellness_blackout_overages (
    blackout_id   INTEGER NOT NULL,
    day_epoch     INTEGER NOT NULL,
    overage_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (blackout_id, day_epoch)
);

CREATE TABLE IF NOT EXISTS wellness_blackout_active (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    blackout_id INTEGER NOT NULL,
    started_at  REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, blackout_id)
);

CREATE TABLE IF NOT EXISTS wellness_slow_mode (
    guild_id               INTEGER NOT NULL,
    user_id                INTEGER NOT NULL,
    triggered_by_cap_id    INTEGER NOT NULL DEFAULT 0,
    triggered_window_start INTEGER NOT NULL DEFAULT 0,
    last_message_ts        REAL NOT NULL DEFAULT 0,
    active_until_ts        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS wellness_streaks (
    guild_id            INTEGER NOT NULL,
    user_id             INTEGER NOT NULL,
    current_days        INTEGER NOT NULL DEFAULT 0,
    personal_best       INTEGER NOT NULL DEFAULT 0,
    streak_start_date   TEXT,
    last_violation_date TEXT,
    current_badge       TEXT NOT NULL DEFAULT '',
    celebrated_badge    TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS wellness_streak_history (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    streak_days INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, day)
);

CREATE TABLE IF NOT EXISTS wellness_partners (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_a       INTEGER NOT NULL,
    user_b       INTEGER NOT NULL,
    requester_id INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL,
    accepted_at  REAL,
    UNIQUE (guild_id, user_a, user_b)
);

CREATE TABLE IF NOT EXISTS wellness_away_rate_limit (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    last_sent_at REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS wellness_exempt_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS wellness_weekly_reports (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    iso_year    INTEGER NOT NULL,
    iso_week    INTEGER NOT NULL,
    week_start  TEXT NOT NULL,
    report_json TEXT NOT NULL,
    ai_text     TEXT NOT NULL DEFAULT '',
    sent_at     REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, iso_year, iso_week)
);

-- ── services/confessions_service.py ──────────────────────────────────

CREATE TABLE IF NOT EXISTS confession_config (
    guild_id            INTEGER PRIMARY KEY,
    dest_channel_id     INTEGER NOT NULL DEFAULT 0,
    log_channel_id      INTEGER NOT NULL DEFAULT 0,
    cooldown_seconds    INTEGER NOT NULL DEFAULT 120,
    max_chars           INTEGER NOT NULL DEFAULT 2000,
    max_attachments     INTEGER NOT NULL DEFAULT 4,
    panic               INTEGER NOT NULL DEFAULT 0,
    replies_enabled     INTEGER NOT NULL DEFAULT 1,
    notify_op_on_reply  INTEGER NOT NULL DEFAULT 0,
    per_day_limit       INTEGER NOT NULL DEFAULT 0,
    launcher_channel_id INTEGER NOT NULL DEFAULT 0,
    launcher_message_id INTEGER NOT NULL DEFAULT 0,
    blocked_user_ids    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS confession_rate_limits (
    guild_id        INTEGER NOT NULL,
    author_id       INTEGER NOT NULL,
    last_confess_at INTEGER NOT NULL DEFAULT 0,
    last_reply_at   INTEGER NOT NULL DEFAULT 0,
    day_key         TEXT NOT NULL DEFAULT '',
    day_count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, author_id)
);

CREATE TABLE IF NOT EXISTS confession_threads (
    guild_id                INTEGER NOT NULL,
    message_id              INTEGER NOT NULL,
    channel_id              INTEGER NOT NULL,
    root_message_id         INTEGER NOT NULL,
    original_author_id      INTEGER NOT NULL,
    notify_original_author  INTEGER NOT NULL DEFAULT -1,
    discord_thread_id       INTEGER NOT NULL DEFAULT 0,
    reply_button_message_id INTEGER NOT NULL DEFAULT 0,
    created_at              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, message_id)
);

CREATE TABLE IF NOT EXISTS confession_emoji_assignments (
    guild_id        INTEGER NOT NULL,
    root_message_id INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    emoji_index     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, root_message_id, user_id)
);

-- ── services/dm_perms_service.py ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS dm_consent_pairs (
    guild_id          INTEGER NOT NULL,
    user_low          INTEGER NOT NULL,
    user_high         INTEGER NOT NULL,
    rel_type          TEXT NOT NULL DEFAULT 'dm',
    reason            TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL DEFAULT 0,
    source_msg_id     INTEGER,
    source_channel_id INTEGER,
    PRIMARY KEY (guild_id, user_low, user_high)
);

CREATE TABLE IF NOT EXISTS dm_requests (
    guild_id      INTEGER NOT NULL,
    requester_id  INTEGER NOT NULL,
    target_id     INTEGER NOT NULL,
    request_type  TEXT NOT NULL DEFAULT 'dm',
    reason        TEXT NOT NULL DEFAULT '',
    message_id    INTEGER,
    channel_id    INTEGER,
    created_at    REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (guild_id, requester_id, target_id)
);

CREATE TABLE IF NOT EXISTS dm_request_channels (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dm_audit_channels (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dm_panel_settings (
    guild_id         INTEGER PRIMARY KEY,
    panel_channel_id INTEGER,
    panel_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS dm_audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    actor_id  INTEGER,
    user_a_id INTEGER,
    user_b_id INTEGER,
    action    TEXT NOT NULL,
    timestamp REAL NOT NULL,
    notes     TEXT
);
