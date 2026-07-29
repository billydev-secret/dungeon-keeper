-- Event Echo (docs/event_echo_spec.md): special events mirrored into main
-- chat with a jump link, so people scrolling the busiest channel find out a
-- game is open without watching every game channel.
--
-- One table backs three otherwise unrelated jobs, because all three are
-- answered by "when did we last echo something":
--
--   * **Dedupe.** The party-game source is a poll loop, so it sees the same
--     open lobby on every tick. A row keyed (guild_id, source, ref) is what
--     stops the second tick re-announcing it.
--   * **Per-type cooldown.** MAX(echoed_at) for one `echo_key`.
--   * **Global floor.** MAX(echoed_at) across every key.
--
-- `suppressed` is the non-obvious column. A game the cooldown rejects still
-- gets a row, flagged — otherwise the sweep would reconsider that same lobby
-- every tick and finally announce it the moment its cooldown expired, which
-- is the worst possible time: the game is now an hour old and probably over.
-- Recording the refusal makes "not echoed" a decision taken once, at open,
-- and it makes the suppression rate visible rather than invisible.
--
-- Cooldown reads therefore filter on `suppressed = 0` — a suppressed row must
-- not itself push the next echo further out, or one busy minute would cascade
-- into an ever-receding window.
CREATE TABLE IF NOT EXISTS event_echo_log (
    echo_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    source     TEXT    NOT NULL,          -- 'party_game' | 'gamebot' | 'discord_event'
    echo_key   TEXT    NOT NULL,          -- per-type cooldown bucket ('mfk', 'cah', …)
    ref        TEXT    NOT NULL,          -- dedupe identity: game_id / message id / event id
    echoed_at  REAL    NOT NULL,          -- unix epoch seconds
    suppressed INTEGER NOT NULL DEFAULT 0 -- 1 = considered and refused by a cooldown
);

-- The dedupe guarantee. INSERT OR IGNORE against this index is what makes a
-- double-tick (or a listener firing twice on an edited message) a no-op
-- rather than a duplicate post.
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_echo_ref
    ON event_echo_log (guild_id, source, ref);

-- Serves both cooldown reads (newest first, per guild) and the age-based
-- prune the loop runs.
CREATE INDEX IF NOT EXISTS idx_event_echo_recent
    ON event_echo_log (guild_id, echoed_at);
