-- Message bookkeeping for the bot-synced #welcome-procedure reference
-- channel (see intake_reference_service). The dashboard-edited blocks are a
-- config value (intake_reference_blocks); this table maps each rendered
-- message position to the Discord message currently holding it, with a
-- content hash so the sync differ can edit in place instead of reposting.
CREATE TABLE IF NOT EXISTS intake_reference_messages (
    guild_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (guild_id, position)
);
