-- Ping Response tracker — an index of "a role ping happened here".
--
-- Deliberately thin. Everything needed to answer "did anyone turn up" is
-- already retained: `messages` (indexed on guild_id, channel_id, ts) and
-- `reaction_log`. So this table records only the *stimulus*; the response is
-- computed at read time by ping_tracker_service against those two tables.
--
-- That is the whole design. Precomputing turnout into columns here would have
-- frozen the response window at whatever constant the sweep used, and would
-- have made backfilled rows and live rows measured by two different code
-- paths. Computing on demand means the panel's window is a live control and
-- history is measured exactly like the present.
--
-- `role_ids` is a JSON array because a single message can ping several roles
-- and each one deserves its own line in the by-role breakdown. Role ids are
-- not member ids, so this is not a LIST_VALUED_MEMBER_COLUMNS blind spot.
--
-- `everyone` covers @everyone/@here, which have no role id but are the
-- loudest ping on the server and would be odd to omit from a ping report.
--
-- `source` starts as 'member' or 'bot' (all the ingest path can tell) and is
-- upgraded in place by senders that know better — the scheduled-game launcher
-- stamps 'game_start' and puts its game_id in `ref`, which is what lets the
-- report say "and this many people actually joined" rather than only "this
-- many people talked".
--
-- PRIVACY: author_id names the member who sent the ping. Cleared by
-- privacy_service.purge_user_data — this is an analytics table with no
-- Art 17(3) ground worth claiming. See docs/data_register.md.

CREATE TABLE IF NOT EXISTS ping_events (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    role_ids    TEXT    NOT NULL DEFAULT '[]',
    everyone    INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT 'member',
    ref         TEXT,
    ts          REAL    NOT NULL
);

-- The report's only access pattern: a guild's pings over a time window.
CREATE INDEX IF NOT EXISTS idx_ping_events_guild_ts
    ON ping_events (guild_id, ts);

-- purge_user_data deletes by author; the erasure path would otherwise scan.
CREATE INDEX IF NOT EXISTS idx_ping_events_author
    ON ping_events (guild_id, author_id);
