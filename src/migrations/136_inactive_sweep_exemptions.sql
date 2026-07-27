-- Migration 136: per-member exemptions from inactivity holds.
--
-- Until now the only members the inactive sweep would never touch were
-- excluded structurally — bots, the owner, admins, mods, anyone already held.
-- There was no way to spare a specific member short of promoting them, which
-- is a poor reason to hand someone moderator. The dashboard's sweep preview
-- makes that gap obvious the moment it lists someone you'd want to keep.
--
-- An exemption is absolute: it removes the member from both sweeps (the
-- background loop and `/inactive sweep`) and also refuses a targeted
-- `/inactive mark`. A moderator who genuinely needs to hold an exempt member
-- lifts the exemption first, so the protection can never be bypassed by
-- accident.
--
-- `added_by`/`added_at` are recorded so the dashboard can show who spared whom
-- and when; they are not read by the sweep itself.

CREATE TABLE IF NOT EXISTS inactive_sweep_exemptions (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    added_by  INTEGER NOT NULL DEFAULT 0,
    added_at  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
