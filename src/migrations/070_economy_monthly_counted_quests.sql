-- Economy quests — monthly cadence and counted ("do it N times") quests.
--
-- 'monthly' joins the qtype CHECK (period = the guild-local calendar month,
-- "YYYY-MM", so it starts on the 1st); widening the CHECK means the SQLite
-- table rebuild again — every column through migration 068 carried over,
-- plus the new `target_count`.
--
-- `target_count` > 1 turns a trigger-kind quest into a counted quest: each
-- distinct occurrence increments `econ_quest_progress` (deduped per
-- occurrence via `econ_quest_progress_marks` so gateway replays and repeat
-- events can't double-count), and the ordinary claim fires when the count
-- reaches the target. Progress rows are per (quest, member, period) — a new
-- period starts a fresh count, no reset sweeps needed.

CREATE TABLE econ_quests_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    title              TEXT    NOT NULL,
    description        TEXT    NOT NULL DEFAULT '',
    qtype              TEXT    NOT NULL CHECK (qtype IN ('daily', 'weekly', 'monthly', 'community', 'event')),
    reward             INTEGER NOT NULL DEFAULT 0,
    signoff            INTEGER NOT NULL DEFAULT 0,
    criteria           TEXT    NOT NULL DEFAULT '',
    starts_at          REAL,
    ends_at            REAL,
    active             INTEGER NOT NULL DEFAULT 0,
    rotate_tag         TEXT    NOT NULL DEFAULT '',
    community_target   INTEGER,
    created_by         INTEGER,
    created_at         REAL    NOT NULL,
    trigger_words      TEXT    NOT NULL DEFAULT '',
    trigger_channel_id INTEGER,
    trigger_kind       TEXT    NOT NULL DEFAULT '',
    target_count       INTEGER NOT NULL DEFAULT 1
);

INSERT INTO econ_quests_new
    (id, guild_id, title, description, qtype, reward, signoff, criteria,
     starts_at, ends_at, active, rotate_tag, community_target, created_by,
     created_at, trigger_words, trigger_channel_id, trigger_kind)
SELECT id, guild_id, title, description, qtype, reward, signoff, criteria,
       starts_at, ends_at, active, rotate_tag, community_target, created_by,
       created_at, trigger_words, trigger_channel_id, trigger_kind
FROM econ_quests;

DROP TABLE econ_quests;
ALTER TABLE econ_quests_new RENAME TO econ_quests;

CREATE INDEX IF NOT EXISTS idx_econ_quests_guild_active
    ON econ_quests (guild_id, active, qtype);

CREATE TABLE IF NOT EXISTS econ_quest_progress (
    quest_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    period    TEXT    NOT NULL,
    current   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (quest_id, user_id, period)
);

CREATE TABLE IF NOT EXISTS econ_quest_progress_marks (
    quest_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    period     TEXT    NOT NULL,
    occurrence TEXT    NOT NULL,
    PRIMARY KEY (quest_id, user_id, period, occurrence)
);
