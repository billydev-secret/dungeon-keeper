-- Multiple ticket panels per guild.
-- Replaces the scalar ticket_panel_channel_id / ticket_panel_message_id config
-- keys with a proper table so any number of panels can coexist in different channels.

CREATE TABLE IF NOT EXISTS ticket_panels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    created_at  REAL    NOT NULL,
    UNIQUE (guild_id, channel_id)
);

-- Back-fill from existing config keys (no-op if they were never set).
INSERT OR IGNORE INTO ticket_panels (guild_id, channel_id, message_id, created_at)
SELECT
    CAST(c1.guild_id AS INTEGER),
    CAST(c1.value    AS INTEGER),
    CAST(c2.value    AS INTEGER),
    UNIXEPOCH()
FROM config c1
JOIN config c2
  ON c2.guild_id = c1.guild_id
 AND c2.key      = 'ticket_panel_message_id'
WHERE c1.key = 'ticket_panel_channel_id'
  AND c1.value NOT IN ('', '0')
  AND c2.value NOT IN ('', '0');
