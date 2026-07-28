-- Migration 139: usage telemetry — which slash commands run, which dashboard
-- panels get opened, by whom, and when.
--
-- Deliberately a new table rather than rows in `audit_log`. audit_log is the
-- moderation trail (vm_channel_delete, ticket_open, …) — 946 rows over ~108
-- days, no indexes, and read as a human-scale history. Command telemetry alone
-- runs a couple hundred rows/day, so within a month it would be ~95% of that
-- table and every analytics GROUP BY would be an unindexed scan across the
-- moderation record. Keeping them apart also means telemetry can be pruned or
-- exported without touching moderation history.
--
-- PRIVACY: user_id is the raw actor, stored indefinitely — a deliberate choice
-- matching the server's existing retain-for-moderation posture (see
-- docs/privacy_spec.md). It is NOT reachable by /delete_me, which only clears
-- Discord-side messages; it IS purged by privacy_service.purge_user_data, the
-- out-of-band hard-erasure path used for legal/GDPR requests.

CREATE TABLE IF NOT EXISTS usage_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    -- 'command' (a slash command invocation) | 'panel_view' (a dashboard
    -- panel mount). One table, because every report wants them side by side.
    kind       TEXT    NOT NULL,
    -- Command name without the leading slash ('bank'), or panel id
    -- ('economy-stats'). Subcommands are stored space-joined as Discord
    -- reports them ('quest board'), so the parent groups naturally.
    name       TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    -- Commands only; NULL for panel views and for commands run in DMs.
    channel_id INTEGER,
    -- 0 when the invocation raised — the erroring rows are the interesting
    -- ones for the "debug" half of this feature, so they are kept, not dropped.
    ok         INTEGER NOT NULL DEFAULT 1,
    extra      TEXT    NOT NULL DEFAULT '{}',
    ts         REAL    NOT NULL
);

-- "What happened in the last N days" — every report starts with this window.
CREATE INDEX IF NOT EXISTS idx_usage_events_ts
    ON usage_events (guild_id, ts);

-- "Per command/panel, how often and when" — the leaderboard and the
-- never-used-command list both group on (kind, name).
CREATE INDEX IF NOT EXISTS idx_usage_events_name
    ON usage_events (guild_id, kind, name, ts);

-- "What did this member use" — also the lookup purge_user_data does.
CREATE INDEX IF NOT EXISTS idx_usage_events_user
    ON usage_events (guild_id, user_id, ts);
