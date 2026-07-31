-- Marqo replaces NudeNet as the NSFW *verdict* engine.
--
-- NudeNet could not see the content the gates exist to catch: a dark,
-- warm-monochrome boudoir photo passed an enforcing SFW gate with zero
-- detections from 320n and a 0.26 MALE_BREAST_EXPOSED from 640m. Marqo scores
-- that image 0.91 against 0.04-0.08 for non-explicit controls.
--
-- Marqo emits a single probability and no bounding boxes, so the two engines
-- now do different jobs:
--
--   Marqo   — the verdict, in EVERY channel. What tipping, spoiler
--             enforcement and SFW prevention act on.
--   NudeNet — labels and boxes, in age-gated channels ONLY, purely to fill
--             nsfw_detections. It never changes what happens to an image.
--
-- Net effect on cost is negative: NudeNet used to run on every image in every
-- channel and its labels were discarded outside age-gated ones.

-- Marqo's probability for the verdict. NULL identifies rows written before the
-- swap, whose verdict came from NudeNet labels instead.
ALTER TABLE nsfw_classifications ADD COLUMN marqo_score REAL;

-- The per-label qualifying set was a detector-threshold concept and has
-- nothing to attach to under a single probability. Leaving the rows would
-- leave a stored preference that nothing enforces.
DELETE FROM config WHERE key = 'nsfw_classifier_labels';

-- Every image the bot destroyed (or, in log mode, would have), so a false
-- positive is reviewable after the fact rather than only in a Discord log
-- channel that may not be configured.
--
-- This is the first table written for uploads in NON-age-gated channels, and
-- it is deliberately not a body-part inventory: no labels, no boxes, no image
-- bytes. Just enough to answer "how often, how confident, and whose image do
-- I owe an apology for".
--
-- author_id is stored rather than joined. Both enforcement paths return from
-- on_message BEFORE message persistence, so a blocked message never gets a
-- `messages` row for authorship to join through — the minimisation used by
-- nsfw_classifications is simply not available here.
--
-- marqo_score IS NULL means the image could not be read at all. Spoiler
-- enforcement deletes on an unreadable image by design (unreadable is treated
-- as maybe-explicit); SFW prevention never does.
CREATE TABLE IF NOT EXISTS nsfw_blocks (
    message_id    INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    author_id     INTEGER NOT NULL,
    filename      TEXT    NOT NULL,
    marqo_score   REAL,               -- NULL = unreadable, not "scored zero"
    surface       TEXT    NOT NULL,   -- 'sfw' | 'spoiler'
    action        TEXT    NOT NULL,   -- 'removed' | 'logged' (log mode)
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (message_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_nsfw_blocks_guild_ts
    ON nsfw_blocks (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nsfw_blocks_author
    ON nsfw_blocks (guild_id, author_id);
