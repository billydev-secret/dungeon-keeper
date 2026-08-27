-- Migration 183: let a server clear a Suggested Setup row for good.
--
-- The Home tile lists features this server hasn't set up, recomputed live from
-- the settings registry on every load. There was no way to say "we've decided
-- not to use that" — the same three rows came back every day, and the next
-- three behind them never got a turn.
--
-- Dismissal is a property of the SERVER, not of the admin who clicked: it
-- records a decision about which features this community wants, so it is keyed
-- by (guild_id, feature_key) with no user column. That is deliberate — nothing
-- here is personal data, so it needs no docs/data_register.md row and
-- purge_user_data has nothing to clear.
--
-- `feature_key` is a settings_registry Feature.slug. Rows are validated against
-- that registry on write, and an unknown slug (a feature that was later
-- renamed or removed) is simply ignored on read, so a stale row is inert
-- rather than an error.
CREATE TABLE IF NOT EXISTS setup_suggestion_dismissals (
    guild_id     INTEGER NOT NULL,
    feature_key  TEXT    NOT NULL,
    dismissed_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, feature_key)
);
