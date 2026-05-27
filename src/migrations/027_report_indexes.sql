-- Speed up greeter-response report queries which previously did full table scans.
-- messages: greeter messages query filters by (guild_id, channel_id) and orders by ts.
CREATE INDEX IF NOT EXISTS idx_messages_guild_channel_ts
    ON messages (guild_id, channel_id, ts);

-- member_events: sessions query filters by guild_id and ts >= cutoff.
-- The composite PK (guild_id, user_id, event_type, ts) can match guild_id but then
-- must scan every guild row to apply the ts range filter.
CREATE INDEX IF NOT EXISTS idx_member_events_guild_ts
    ON member_events (guild_id, ts);
