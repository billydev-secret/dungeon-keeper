-- Migration 145: a DB-backed audit trail for every anonymous member-facing
-- surface (docs/anon_audit_spec.md).
--
-- WHY THIS EXISTS: before this, the anonymous games (AMA, FFA, Hot Takes,
-- Fantasies) audited only through games/utils/audit.py, which posts an embed
-- to the channel in games_audit_channel and writes nothing. That channel is
-- unset by default, so an anonymous AMA question left no trace anywhere — and
-- log.txt is truncated on every boot, so there was no fallback. Clapback's
-- anonymous mode, WYR anonymous votes/posed questions, and Compliment had no
-- audit at all. Confessions, Whisper and Guess were already fine — their own
-- operational tables plus admin dashboard panels (confession_threads,
-- whispers, and guess_audit_log, which already records /guess confess via
-- guess_cog._do_audit). They are deliberately NOT migrated onto this table:
-- those tables are load-bearing for the features themselves, so putting them
-- under a retention purge would break thread identity, whisper state and
-- round history.
--
-- Deliberately a new table rather than rows in `audit_log`, for the same
-- reason migration 139 kept usage_events separate: audit_log is the
-- human-scale moderation trail (vm_channel_delete, ticket_open, …) with no
-- retention policy, and WYR anonymous votes alone would swamp it within
-- weeks and turn every moderation GROUP BY into a scan across game telemetry.
-- Separation also lets this table be purged on a schedule while the
-- moderation record is kept forever.
--
-- PRIVACY — read before adding columns:
--
--   * actor_id is the real author behind an anonymous post. That is the whole
--     point of the table (a mod must be able to trace an abusive anonymous
--     submission), and it is what makes these rows sensitive. Admin-gated on
--     the dashboard; there is no member-facing read path.
--
--   * There is NO content column, and one must not be added. This matches
--     confession_threads exactly: store the message pointer, and let the
--     reader LEFT JOIN messages for the text. Content therefore exists only
--     at guild storage level 'all' (message_store.guild_retains_content;
--     the default is 'none') and disappears if the level is lowered or
--     purge_guild_message_content runs. That is the intended posture, not a
--     gap — the audit record is who and when, with what available
--     opportunistically through the same content-retention setting that
--     governs every other message on the server.
--
--   * Consequence, accepted knowingly: surfaces that never produce a guild
--     message have no recoverable text at all. A screened AMA question the
--     host rejects is DM'd to the host and never posted; a WYR question is
--     queued rather than posted; Hot Takes and Fantasies entries are held in
--     the game payload until the reveal. Those rows record who and when only.
--
--   * These rows are cleared by privacy_service.purge_user_data (the
--     out-of-band hard-erasure path), which matches actor_id and target_id.

CREATE TABLE IF NOT EXISTS anon_audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    -- Feature slug: 'ama' | 'ffa' | 'hottakes' | 'fantasies' | 'clapback'
    -- | 'wyr' | 'compliment'. Matches games.constants game_type so GAME_ICONS
    -- keeps working.
    feature    TEXT    NOT NULL,
    -- What happened, namespaced per feature ('question_asked',
    -- 'question_answered', 'question_passed', 'hot_seat_skipped', 'vote', …).
    event      TEXT    NOT NULL,
    -- Who performed the action. Usually the real member behind an anonymous
    -- post; on moderation events (a host approving/rejecting a screened AMA
    -- question, a mod revealing WYR voters) it is the moderator instead.
    actor_id   INTEGER NOT NULL,
    -- Who the action concerned, where that is meaningful: the AMA hot seat
    -- being asked, the anonymous asker whose question was rejected, the new
    -- hot seat. NULL otherwise.
    target_id  INTEGER,
    -- games_active_games.game_id (a uuid4 string), so a whole session's
    -- anonymous traffic can be pulled together. NULL for non-game features.
    game_id    TEXT,
    -- Pointer into `messages` for the LEFT JOIN that recovers content, and
    -- for rebuilding a Discord deep link. NULL when the event produced no
    -- guild message (rejected screened questions, DMs, image-card posts).
    message_id INTEGER,
    channel_id INTEGER,
    -- Small structured details that are metadata, not content: the WYR option
    -- index voted for, the AMA question index, whether Clapback's anonymous
    -- mode was on. Never the submitted text.
    extra      TEXT    NOT NULL DEFAULT '{}',
    created_at REAL    NOT NULL
);

-- Two indexes, matching the two shapes the panel actually issues. Both lead
-- with guild_id and end in created_at so the retention purge
-- (guild_id = ? AND created_at < ?) is a range seek rather than a scan.
--
-- The unfiltered "latest N for this guild" query, and the purge.
CREATE INDEX IF NOT EXISTS idx_anon_audit_log_guild
    ON anon_audit_log (guild_id, created_at);

-- The feature-filtered view, which is how a mod narrows past WYR vote volume.
CREATE INDEX IF NOT EXISTS idx_anon_audit_log_feature
    ON anon_audit_log (guild_id, feature, created_at);

-- No (guild_id, actor_id) index on purpose. "What has this member posted
-- anonymously" is a real query but a rare, human-initiated one, and retention
-- bounds the table to ~90 days of one guild's traffic — cheap to post-filter.
-- Migration 139 is the cautionary tale: a third index there made SQLite prefer
-- it and silently lose the time bound on the common query.

CREATE TABLE IF NOT EXISTS anon_audit_config (
    guild_id       INTEGER PRIMARY KEY,
    -- Days of history to keep. 0 means keep forever (no purge). Default 90 is
    -- long enough to investigate a report that surfaces weeks later, short
    -- enough that a deanonymising table is not an indefinite archive.
    retention_days INTEGER NOT NULL DEFAULT 90
);
