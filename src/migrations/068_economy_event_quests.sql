-- Economy quests — the 'event' quest type and the Photo Challenge card registry.
--
-- An event quest is never member-claimed: a bot listener pays it when its
-- trigger fires. `trigger_kind` names the trigger ('photo_reply' is the only
-- kind in v1 — a Discord reply to a Photo Challenge card carrying an image).
-- The claim `period` for an event quest is supplied by the listener, keyed to
-- the triggering thing (photo cards use "photo:<game_id>"), so payouts are
-- deduped per member per card with no time gate.
--
-- Widening the qtype CHECK requires the SQLite table rebuild; every column
-- through migration 067 is carried over verbatim.
--
-- `econ_photo_cards` maps a posted Photo Challenge card message to its game —
-- games_game_history drops message_id on archive, so the reply listener needs
-- its own registry. Rows are kept indefinitely: replies to old cards still pay
-- (no time gate, by design).

CREATE TABLE econ_quests_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    title              TEXT    NOT NULL,
    description        TEXT    NOT NULL DEFAULT '',
    qtype              TEXT    NOT NULL CHECK (qtype IN ('daily', 'weekly', 'community', 'event')),
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
    trigger_kind       TEXT    NOT NULL DEFAULT ''
);

INSERT INTO econ_quests_new
    (id, guild_id, title, description, qtype, reward, signoff, criteria,
     starts_at, ends_at, active, rotate_tag, community_target, created_by,
     created_at, trigger_words, trigger_channel_id)
SELECT id, guild_id, title, description, qtype, reward, signoff, criteria,
       starts_at, ends_at, active, rotate_tag, community_target, created_by,
       created_at, trigger_words, trigger_channel_id
FROM econ_quests;

DROP TABLE econ_quests;
ALTER TABLE econ_quests_new RENAME TO econ_quests;

CREATE INDEX IF NOT EXISTS idx_econ_quests_guild_active
    ON econ_quests (guild_id, active, qtype);

CREATE TABLE IF NOT EXISTS econ_photo_cards (
    message_id  INTEGER PRIMARY KEY,   -- the card message the member replies to
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    game_id     TEXT    NOT NULL,      -- games_game_history.game_id; period key
    prompt      TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_econ_photo_cards_guild
    ON econ_photo_cards (guild_id, created_at);
