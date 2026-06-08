CREATE TABLE IF NOT EXISTS bump_tracker_config (
    guild_id          INTEGER PRIMARY KEY,
    channel_id        INTEGER NOT NULL DEFAULT 0,
    role_id           INTEGER NOT NULL DEFAULT 0,
    widget_message_id INTEGER NOT NULL DEFAULT 0,
    enabled           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bump_tracker_sites (
    guild_id         INTEGER NOT NULL,
    site_name        TEXT    NOT NULL,
    cooldown_seconds INTEGER NOT NULL,
    PRIMARY KEY (guild_id, site_name)
);

CREATE TABLE IF NOT EXISTS bump_tracker_log (
    guild_id  INTEGER NOT NULL,
    site_name TEXT    NOT NULL,
    bumped_at REAL    NOT NULL,
    notified  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, site_name)
);
