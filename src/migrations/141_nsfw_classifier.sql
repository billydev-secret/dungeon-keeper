-- Shared NSFW image classifier — metrics store
-- (docs/plans/nsfw-classifier-and-reaction-tips.md, Stage 1).
--
-- One classification per (message, attachment), shared by every consumer:
-- reaction tipping, spoiler enforcement, and SFW nudity prevention all fire
-- off the same on_message, so the verdict is computed once and reused.
--
-- Coverage and recording deliberately differ. Classification runs on
-- attachments in EVERY channel, because SFW prevention needs a verdict
-- everywhere — but rows land here ONLY for uploads in Discord-age-gated
-- (is_nsfw) channels, so no dataset is built out of general chat.
--
-- No author_id column on purpose: authorship joins through messages.
-- nsfw_detections is the most sensitive table this bot holds (a labelled
-- body-part inventory of members' uploads), so it stores the minimum that
-- answers a tuning question and is only ever surfaced admin-gated.
--
-- `threshold` and `label_set` are stored per row rather than read from config
-- at query time: without them, rows written before a retune become
-- uninterpretable and "what would 0.4 have changed?" stops being answerable.

CREATE TABLE IF NOT EXISTS nsfw_classifications (
    message_id    INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    verdict       INTEGER NOT NULL,   -- 1 = explicit, 0 = not
    top_label     TEXT,               -- highest-scoring qualifying label, if any
    top_score     REAL,
    model         TEXT    NOT NULL,   -- '320n' — which weights produced this
    threshold     REAL    NOT NULL,   -- what was applied, for retro-tuning
    label_set     TEXT    NOT NULL,   -- comma-joined qualifying labels, sorted
    inference_ms  INTEGER NOT NULL,
    bytes         INTEGER,            -- source image size, for bandwidth review
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (message_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_nsfw_classifications_guild_ts
    ON nsfw_classifications (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nsfw_classifications_verdict
    ON nsfw_classifications (guild_id, verdict);

-- Every detection above the model's own floor, INCLUDING ones that did not
-- qualify — a threshold sweep is only possible if the near-misses were kept.
CREATE TABLE IF NOT EXISTS nsfw_detections (
    message_id    INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    label         TEXT    NOT NULL,
    score         REAL    NOT NULL,
    x1 INTEGER, y1 INTEGER, x2 INTEGER, y2 INTEGER
);

CREATE INDEX IF NOT EXISTS idx_nsfw_detections_msg
    ON nsfw_detections (message_id, attachment_id);
CREATE INDEX IF NOT EXISTS idx_nsfw_detections_label
    ON nsfw_detections (label);
