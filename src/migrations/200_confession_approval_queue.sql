-- Migration 200: mod-approve mode for confession submissions (2026-09-02).
--
-- Todo #136. With `require_approval` on, a confession no longer goes straight
-- to the destination channel: it waits in `confession_pending` until a
-- moderator approves it from the sticky todo board. Off by default -- turning
-- it on is a dashboard decision, and a guild that never does keeps today's
-- behaviour byte for byte.
--
-- PER-USER DATA. `confession_pending` holds a member's confession *text*
-- alongside their real id, which no other confessions table does:
-- `anon_audit_log` stores no content at all (migration 145) and recovers it by
-- joining the general `messages` table, and `confession_threads` keeps routing
-- metadata only. It is therefore the most sensitive row the feature writes, and
-- it is deliberately transient -- deleted the moment a mod approves or rejects,
-- and swept at seven days if nobody ever does. Seven, not thirty, because
-- manual.html promises members that "the link between a confession and its
-- author self-destructs after 7 days", and a pending row is exactly that link.
-- See docs/data_register.md.
--
-- `author_id` is already one of privacy_service.SUBJECT_ID_COLUMNS, and the
-- table is added to the purge list in the same commit, so an erasure request
-- clears anything still waiting.

ALTER TABLE confession_config
    ADD COLUMN require_approval INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS confession_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    notify_original_author INTEGER NOT NULL DEFAULT -1,
    created_at INTEGER NOT NULL DEFAULT 0
);

-- The board reads "oldest pending first, this guild" on every repaint.
CREATE INDEX IF NOT EXISTS idx_confession_pending_queue
    ON confession_pending(guild_id, created_at);
