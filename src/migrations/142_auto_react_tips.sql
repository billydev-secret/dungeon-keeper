-- Auto React tipping — rule flag + placement receipts
-- (docs/plans/nsfw-classifier-and-reaction-tips.md, Stage 4).
--
-- `tips_enabled` is per-rule, not per-guild, so Auto React stays usable as
-- plain emoji decoration on channels nobody wants monetised. Defaulting to 0
-- means every existing rule keeps its current behavior exactly.
--
-- auto_react_placements records what the bot actually placed. That receipt is
-- what makes a rung "live": only emoji the bot itself put on a specific
-- message can be tipped. Keying on the configured emoji set instead would let
-- anyone paste a rung onto a text post, an old message, or an image the
-- classifier rejected, and turn it into a payment target nothing approved.
--
-- The alternative — fetching the message and checking reaction.me on every
-- reaction event — costs an API round trip at ~1,050 events/day, and this
-- table doubles as the record of which posts qualified.

ALTER TABLE auto_react_config ADD COLUMN tips_enabled INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS auto_react_placements (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,   -- the tip recipient, resolved at placement
    emojis     TEXT    NOT NULL,   -- comma-joined, exactly what was placed
    created_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_auto_react_placements_channel
    ON auto_react_placements (guild_id, channel_id, created_at);
