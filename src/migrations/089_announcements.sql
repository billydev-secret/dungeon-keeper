-- Timed announcements: dashboard-queued one-shot channel posts.
-- Wall-clock fields (post_date, post_time_min) are the source of truth so a later
-- tz-offset change keeps the intended local time; post_at is a derived UTC-epoch
-- cache the polling loop fires on. NULL post_at = draft (inert by construction).
CREATE TABLE IF NOT EXISTS announcements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    title            TEXT    NOT NULL DEFAULT '',
    body             TEXT    NOT NULL DEFAULT '',      -- markdown embed description
    image_url        TEXT,                             -- optional embed image
    accent_hex       TEXT,                             -- 6-hex override; NULL = server branding
    plain_text       TEXT,                             -- optional line above the embed (pings live here)
    mention_kind     TEXT    NOT NULL DEFAULT 'none',  -- none | role | everyone
    mention_role_id  INTEGER,
    post_date        TEXT,                             -- guild-local YYYY-MM-DD
    post_time_min    INTEGER,                          -- minutes since local midnight
    post_at          REAL,                             -- derived UTC-epoch cache; NULL = draft
    status           TEXT    NOT NULL DEFAULT 'draft', -- draft | scheduled | sent | error
    sent_channel_id  INTEGER,                          -- channel actually posted to
    sent_message_id  INTEGER,
    sent_at          REAL,
    error            TEXT,
    created_by       INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcements_due
    ON announcements(status, post_at);
