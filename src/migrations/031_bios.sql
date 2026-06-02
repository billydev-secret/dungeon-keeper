-- 030_bios.sql
-- Member bios cog — schema for templates, fields, question pool, and posted bios.

CREATE TABLE IF NOT EXISTS bio_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL UNIQUE,
    version     INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bio_fields (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id  INTEGER NOT NULL REFERENCES bio_templates(id),
    key          TEXT    NOT NULL DEFAULT '',
    label        TEXT    NOT NULL,
    field_type   TEXT    NOT NULL CHECK (field_type IN ('short', 'paragraph', 'choice')),
    choices      TEXT    NOT NULL DEFAULT '[]',
    required     INTEGER NOT NULL DEFAULT 0,
    is_headline  INTEGER NOT NULL DEFAULT 0,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    max_len      INTEGER NOT NULL DEFAULT 1024,
    CHECK (is_headline = 0 OR field_type = 'short')
);

CREATE INDEX IF NOT EXISTS idx_bio_fields_template_order ON bio_fields (template_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_bio_fields_active         ON bio_fields (template_id, active);

CREATE TABLE IF NOT EXISTS bio_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    prompt      TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    weight      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bio_questions_guild_active ON bio_questions (guild_id, active);

CREATE TABLE IF NOT EXISTS bios (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_bios_guild ON bios (guild_id);

CREATE TABLE IF NOT EXISTS bio_field_values (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    field_id     INTEGER NOT NULL REFERENCES bio_fields(id),
    field_label  TEXT    NOT NULL,
    value        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, guild_id, field_id)
);

CREATE INDEX IF NOT EXISTS idx_bio_field_values_user ON bio_field_values (user_id, guild_id);

CREATE TABLE IF NOT EXISTS bio_answers (
    user_id        INTEGER NOT NULL,
    guild_id       INTEGER NOT NULL,
    slot           INTEGER NOT NULL,
    question_id    INTEGER NOT NULL REFERENCES bio_questions(id),
    question_text  TEXT    NOT NULL,
    answer         TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, guild_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_bio_answers_user ON bio_answers (user_id, guild_id);
