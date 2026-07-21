-- Docs — single-source markdown documents rendered as embeds in many places.
--
-- The problem this solves: rules, moderator FAQ, staff bios, etc. get posted in
-- several channels but must be maintained in one place. A `doc` holds the
-- canonical markdown; a `doc_placement` records each channel it's posted to; and
-- `doc_placement_messages` tracks the ordered message ids that render it there
-- (a long doc spans several messages, so sync reconciles add/remove as the doc
-- grows or shrinks). Editing the doc (dashboard or /docs) re-renders every
-- placement in place.

CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    doc_key     TEXT    NOT NULL,             -- slug, e.g. "rules", "mod-faq"
    title       TEXT    NOT NULL DEFAULT '',  -- human title / first-embed heading fallback
    body_md     TEXT    NOT NULL DEFAULT '',  -- canonical markdown source
    accent      TEXT    NOT NULL DEFAULT '',  -- optional #hex override; '' = branding accent
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    updated_by  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, doc_key)
);

CREATE TABLE IF NOT EXISTS doc_placements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    UNIQUE (doc_id, channel_id)
);

CREATE TABLE IF NOT EXISTS doc_placement_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    placement_id  INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    position      INTEGER NOT NULL             -- 0-based order within the placement
);

CREATE INDEX IF NOT EXISTS idx_doc_placements_doc
    ON doc_placements (doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_placement_messages_pl
    ON doc_placement_messages (placement_id, position);
