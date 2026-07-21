-- Greeting Watch — flag "good morning" / "hello" messages in main chat that
-- go unanswered, so nobody falls through the cracks.
--
-- A row is written live from on_message when a member posts a greeting in a
-- watched channel (message content is judged in-memory there, because the
-- default "none" storage level drops it before it reaches the DB). The
-- background loop later checks whether anyone replied to or mentioned the
-- greeter within the configured window; if not, it DMs the configured notify
-- user. `resolved_at` NULL means the greeting is still pending a verdict;
-- `outcome` records what the sweep decided ('acknowledged' / 'unanswered' /
-- 'skipped' when the feature was turned off mid-window).

CREATE TABLE IF NOT EXISTS greeting_watch (
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    created_ts  INTEGER NOT NULL,
    resolved_at INTEGER,
    outcome     TEXT,
    PRIMARY KEY (guild_id, message_id)
);

-- The sweep scans for still-pending rows past their window; a partial index
-- keyed on the pending predicate keeps that scan cheap as resolved rows pile up.
CREATE INDEX IF NOT EXISTS idx_greeting_watch_pending
    ON greeting_watch (guild_id, created_ts)
    WHERE resolved_at IS NULL;

-- Dedup guard: "was there already a live greeting from this author here?" is a
-- per (guild, channel, author) lookup restricted to pending rows.
CREATE INDEX IF NOT EXISTS idx_greeting_watch_author_pending
    ON greeting_watch (guild_id, channel_id, author_id)
    WHERE resolved_at IS NULL;
