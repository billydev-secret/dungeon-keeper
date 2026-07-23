-- Frozen ⚡ weekly spotlight kind per (guild, ISO week).
--
-- spotlight_kind picks the week's double-paying trigger kind deterministically
-- from the guild's DISTINCT active kinds via a (guild, week) digest. The digest
-- is stable, but the underlying kind list is NOT: dashboard toggles and
-- rotate_pool change which kinds are active mid-week, and len(kinds) moves the
-- modulo — so the chosen kind silently shifted after the week's announcement
-- (posted once, with a ping) had already advertised a different one, and the
-- claim-time credit path doubled a kind nobody was told about.
--
-- Fix: persist the chosen kind the first time it is resolved for a week (the
-- week-roll announcement is normally that first read) and return the stored
-- value on every later read, so every surface agrees with the announcement all
-- week. A week with fewer than 2 active kinds stores nothing (no spotlight),
-- so a later second kind can still switch it on — the on/off transition is not
-- frozen, only the choice between kinds.
CREATE TABLE IF NOT EXISTS econ_spotlight_kind (
    guild_id INTEGER NOT NULL,
    iso_week TEXT    NOT NULL,   -- "YYYY-Www"
    kind     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, iso_week)
) WITHOUT ROWID;
