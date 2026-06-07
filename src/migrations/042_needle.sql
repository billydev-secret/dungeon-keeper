CREATE TABLE IF NOT EXISTS needle_channels (
    guild_id            INTEGER NOT NULL,
    channel_id          INTEGER NOT NULL,
    title_type          TEXT    NOT NULL DEFAULT 'first_fifty',
    custom_title        TEXT    NOT NULL DEFAULT '',
    include_bots        INTEGER NOT NULL DEFAULT 0,
    slowmode            INTEGER NOT NULL DEFAULT 0,
    delete_behavior     TEXT    NOT NULL DEFAULT 'archive_if_empty',
    reply_type          TEXT    NOT NULL DEFAULT 'default',
    custom_reply        TEXT    NOT NULL DEFAULT '',
    status_reactions    INTEGER NOT NULL DEFAULT 0,
    archive_immediately INTEGER NOT NULL DEFAULT 0,
    default_reactions   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, channel_id)
);
