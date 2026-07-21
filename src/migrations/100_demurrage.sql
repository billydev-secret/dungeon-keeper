-- Weekly hoard tax (demurrage) — the sink that needs no buyer.
--
-- Every other sink sells something, so a member who wants nothing simply
-- accumulates forever. At the guild's ISO-week roll, wallets above a
-- configured threshold lose a configured percent of the EXCESS only (the
-- threshold is a protected floor). Rate 0 is the dark-launch default; the
-- Sinks page is the launch switch.
--
-- This table's (guild, week) primary key is the exactly-once claim, the
-- raffle-draws pattern: the sweep row is inserted BEFORE any wallet is
-- debited, so a crash-and-replay of the week roll collects nothing twice.
-- taxed_members/total are filled in after the sweep for the metrics page.

CREATE TABLE IF NOT EXISTS econ_demurrage_sweeps (
    guild_id      INTEGER NOT NULL,
    iso_week      TEXT    NOT NULL,   -- the week that just CLOSED
    taxed_members INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,  -- currency evaporated
    created_at    REAL    NOT NULL,
    PRIMARY KEY (guild_id, iso_week)
);
