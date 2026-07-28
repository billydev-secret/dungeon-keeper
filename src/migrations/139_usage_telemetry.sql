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

-- Two indexes, deliberately. Every extra index is also a write cost on the
-- hot path of every slash command, and — measured, not assumed — a narrower
-- index here actively *hurts*, because SQLite prefers it and then loses the
-- time bound (see below).

-- `totals` is the only query with no `kind` predicate.
CREATE INDEX IF NOT EXISTS idx_usage_events_ts
    ON usage_events (guild_id, ts);

-- Everything else. `ts` sits BEFORE name/user_id on purpose: put a
-- high-cardinality column ahead of it and `ts >= ?` stops being a range seek
-- and degrades to a post-filter, so a 7-day report reads every row ever
-- recorded for that kind. Rows are kept forever (no retention policy — see
-- docs/usage_telemetry_spec.md), so that turns report cost into a function of
-- total history rather than of the window. The trailing columns make it
-- covering for the per-name and per-user rollups.
--
-- Measured at 90k rows: per-name rollup 70.5ms -> 4.5ms, per-user 15.8ms ->
-- 3.9ms, and both now scale with the window instead of with all history.
--
-- A `(guild_id, kind, name)` index was tried and REMOVED: it makes the
-- distinct-name lookups for the never-used lists ~100x faster in isolation,
-- but the planner then prefers it for the per-name rollup too (it avoids a
-- sort) and that query goes back to 70ms of full-history scan. Net across a
-- whole report render it was ~188ms vs ~83ms. The never-used lists genuinely
-- have to read all history — that is their definition — so they pay a scan
-- here rather than making every other query pay one.
CREATE INDEX IF NOT EXISTS idx_usage_events_window
    ON usage_events (guild_id, kind, ts, name, user_id, ok);
