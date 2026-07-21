-- Migration 102: turn the XP → coin conversion faucet off in prod.
--
-- The XP→coins pipeline is being retired to a dormant knob: earning XP no
-- longer mints currency. The code default for `econ_xp_per_coin` is now 0
-- (off), but guilds that had a rate explicitly stored keep that stored value,
-- so the new default alone wouldn't touch them. This zeroes any stored rate so
-- the faucet actually goes dormant on the next restart.
--
-- Fully reversible without a code change: an admin re-enables it by setting a
-- positive rate on the Income Sources panel (the day-roll driver skips the
-- conversion while the rate is 0). Idempotent; a no-op where no rate is stored.

UPDATE config
SET value = '0'
WHERE key = 'econ_xp_per_coin';
