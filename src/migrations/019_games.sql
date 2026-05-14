-- 019_games.sql
-- Adds all PoppyBot game tables to the shared DungeonKeeper database.
-- Table names are prefixed with `games_` to avoid future collisions,
-- except for `legitlibs_*` which are already namespaced.

-- Core game infrastructure

CREATE TABLE IF NOT EXISTS games_consent (
    user_id     INTEGER PRIMARY KEY,
    tod_consent BOOLEAN DEFAULT FALSE,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games_allowed_channels (
    channel_id  INTEGER PRIMARY KEY,
    added_by    INTEGER NOT NULL,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games_active_games (
    game_id     TEXT PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    game_type   TEXT NOT NULL,
    host_id     INTEGER NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games_question_bank (
    question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    game_type     TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT 'sfw',
    question_text TEXT NOT NULL,
    added_by      INTEGER NOT NULL,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_games_qb_type_cat ON games_question_bank(game_type, category);

CREATE TABLE IF NOT EXISTS games_game_history (
    history_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      TEXT NOT NULL,
    game_type    TEXT NOT NULL,
    channel_id   INTEGER NOT NULL,
    host_id      INTEGER NOT NULL,
    player_count INTEGER DEFAULT 0,
    round_count  INTEGER DEFAULT 0,
    payload      TEXT DEFAULT '{}',
    started_at   TIMESTAMP NOT NULL,
    ended_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_games_gh_channel ON games_game_history(channel_id, ended_at DESC);

CREATE TABLE IF NOT EXISTS games_session_tracker (
    session_id   TEXT PRIMARY KEY,
    channel_id   INTEGER NOT NULL,
    started_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_game_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    game_ids     TEXT NOT NULL DEFAULT '[]',
    player_ids   TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_games_st_channel ON games_session_tracker(channel_id);

CREATE TABLE IF NOT EXISTS games_timer_defaults (
    game_type     TEXT PRIMARY KEY,
    default_timer INTEGER NOT NULL DEFAULT 60
);

CREATE TABLE IF NOT EXISTS games_audit_channel (
    guild_id    INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    set_by      INTEGER NOT NULL,
    set_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games_portal_access (
    user_id     INTEGER PRIMARY KEY,
    granted_by  INTEGER NOT NULL,
    granted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- LegitLibs

CREATE TABLE IF NOT EXISTS legitlibs_blank_axes (
    axis        TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    parent_pos  TEXT,
    min_tier    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (axis, value, parent_pos)
);

CREATE TABLE IF NOT EXISTS legitlibs_blank_prompts (
    pos         TEXT    NOT NULL,
    domain      TEXT,
    form        TEXT,
    tier        INTEGER NOT NULL,
    prompt      TEXT    NOT NULL,
    examples    TEXT    NOT NULL,
    length_cap  INTEGER,
    PRIMARY KEY (pos, domain, form, tier)
);

CREATE TABLE IF NOT EXISTS legitlibs_templates (
    template_id  TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'draft',
    player_min   INTEGER NOT NULL DEFAULT 2,
    player_max   INTEGER NOT NULL DEFAULT 99,
    blanks       TEXT NOT NULL DEFAULT '[]',
    author_id    INTEGER NOT NULL,
    notes        TEXT DEFAULT '',
    use_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMP,
    report_count INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ll_templates_tier ON legitlibs_templates(tier, status);

CREATE TABLE IF NOT EXISTS legitlibs_revisions (
    revision_id    TEXT PRIMARY KEY,
    template_id    TEXT NOT NULL,
    editor_id      INTEGER NOT NULL,
    body           TEXT NOT NULL,
    metadata       TEXT NOT NULL,
    change_summary TEXT DEFAULT '',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS legitlibs_reports (
    report_id          TEXT PRIMARY KEY,
    game_id            TEXT NOT NULL,
    submission_content TEXT NOT NULL,
    reporter_id        INTEGER NOT NULL,
    reported_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS legitlibs_channel_config (
    channel_id INTEGER PRIMARY KEY,
    max_tier   INTEGER NOT NULL DEFAULT 4,
    set_by     INTEGER NOT NULL,
    set_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS legitlibs_recent_use (
    guild_id    INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    used_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, template_id)
);

-- Seed: legitlibs_blank_axes
-- POS rows (parent_pos is NULL)
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'noun',        NULL, 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'verb',        NULL, 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'adjective',   NULL, 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'adverb',      NULL, 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'exclamation', NULL, 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'number',      NULL, 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('pos', 'wildcard',    NULL, 1);
-- Noun domains
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('domain', 'place',  'noun', 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('domain', 'person', 'noun', 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('domain', 'body',   'noun', 2);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('domain', 'kink',   'noun', 4);
-- Verb domains
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('domain', 'intimate', 'verb', 3);
-- Verb forms
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('form', 'ing',        'verb', 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('form', 'past',       'verb', 1);
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('form', 'infinitive', 'verb', 1);
-- Noun forms
INSERT OR IGNORE INTO legitlibs_blank_axes (axis, value, parent_pos, min_tier) VALUES ('form', 'plural', 'noun', 1);

-- Seed: legitlibs_blank_prompts
-- NULL values in domain/form columns are written as NULL literals.

-- noun, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', NULL, NULL, 1, 'a noun', '["dog", "chair", "umbrella"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', NULL, NULL, 2, 'a noun', '["dog", "chair", "umbrella"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', NULL, NULL, 3, 'a noun', '["dog", "chair", "umbrella"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', NULL, NULL, 4, 'a noun', '["dog", "chair", "umbrella"]', 100);

-- verb, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, NULL, 1, 'a verb (base form)', '["run", "eat", "sleep"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, NULL, 2, 'a verb', '["run", "eat", "sleep"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, NULL, 3, 'a verb', '["grind", "devour", "ravage"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, NULL, 4, 'a verb', '["fuck", "rail", "dominate"]', 100);

-- verb, domain=NULL, form='ing'
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, 'ing', 1, 'a verb (-ing form)', '["running", "eating", "sleeping"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, 'ing', 2, 'a verb (-ing form)', '["running", "eating", "sleeping"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, 'ing', 3, 'a verb (-ing form)', '["grinding", "devouring", "writhing"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', NULL, 'ing', 4, 'a verb (-ing form)', '["fucking", "railing", "dominating"]', 100);

-- adjective, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adjective', NULL, NULL, 1, 'an adjective', '["fluffy", "enormous", "mysterious"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adjective', NULL, NULL, 2, 'an adjective', '["sweaty", "breathless", "trembling"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adjective', NULL, NULL, 3, 'a spicy adjective', '["naked", "soaking wet", "throbbing"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adjective', NULL, NULL, 4, 'an adjective (go unhinged)', '["obscene", "depraved", "absolutely feral"]', 100);

-- adverb, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adverb', NULL, NULL, 1, 'an adverb', '["slowly", "desperately", "aggressively"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adverb', NULL, NULL, 2, 'an adverb', '["slowly", "desperately", "aggressively"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adverb', NULL, NULL, 3, 'an adverb', '["hungrily", "recklessly", "violently"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('adverb', NULL, NULL, 4, 'an adverb', '["recklessly", "hungrily", "desperately"]', 100);

-- exclamation, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('exclamation', NULL, NULL, 1, 'an exclamation', '["Oh my!", "Wow!", "Yikes!"]', 80);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('exclamation', NULL, NULL, 2, 'an exclamation', '["Oh my!", "Wow!", "Yikes!"]', 80);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('exclamation', NULL, NULL, 3, 'an exclamation', '["Oh fuck!", "Sweet lord!", "Not again!"]', 80);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('exclamation', NULL, NULL, 4, 'an exclamation', '["OH GOD!", "WHAT THE—", "I''m calling the police!"]', 80);

-- number, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('number', NULL, NULL, 1, 'a number', '["3", "42", "100"]', 20);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('number', NULL, NULL, 2, 'a number', '["3", "42", "100"]', 20);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('number', NULL, NULL, 3, 'a number', '["3", "42", "100"]', 20);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('number', NULL, NULL, 4, 'a number', '["69", "420", "666"]', 20);

-- wildcard, domain=NULL, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('wildcard', NULL, NULL, 1, 'anything you want', '["surprise us", "be creative"]', 200);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('wildcard', NULL, NULL, 2, 'anything — make it interesting', '["surprise us", "be creative"]', 200);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('wildcard', NULL, NULL, 3, 'anything — make it spicy', '["go filthy", "be cursed"]', 200);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('wildcard', NULL, NULL, 4, 'anything — absolutely no limits', '["be unhinged", "make it a crime"]', 200);

-- noun + place
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'place', NULL, 1, 'a place', '["the grocery store", "the park", "a dentist office"]', 150);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'place', NULL, 2, 'a place', '["the grocery store", "the park", "a dentist office"]', 150);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'place', NULL, 3, 'a place', '["the bedroom", "a hot tub", "a dark alley"]', 150);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'place', NULL, 4, 'a place (the weirder the better)', '["a McDonald''s bathroom", "the void", "your parent''s basement"]', 150);

-- noun + person
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'person', NULL, 1, 'a person''s name', '["Susan", "Dave", "Marcus"]', 80);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'person', NULL, 2, 'a person''s name', '["Susan", "Dave", "Marcus"]', 80);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'person', NULL, 3, 'a person''s name', '["Susan", "Dave", "Marcus"]', 80);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'person', NULL, 4, 'a person''s name', '["Susan", "Dave", "Marcus"]', 80);

-- noun + body
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'body', NULL, 2, 'a body part (keep it PG-13)', '["shoulder", "knee", "earlobe"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'body', NULL, 3, 'a body part', '["breast", "butt", "inner thigh"]', 100);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'body', NULL, 4, 'a body part (go wild)', '["dick", "ass", "nipple"]', 100);

-- noun + kink
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('noun', 'kink', NULL, 4, 'a kink or fetish (noun)', '["rope", "collar", "blindfold"]', 100);

-- verb + intimate, form=NULL
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', 'intimate', NULL, 3, 'an intimate act', '["make out", "grind", "trace their jaw"]', 150);
INSERT OR IGNORE INTO legitlibs_blank_prompts (pos, domain, form, tier, prompt, examples, length_cap) VALUES ('verb', 'intimate', NULL, 4, 'an intimate act (the more unhinged the better)', '["rail into next week", "tie up", "edge for hours"]', 150);
