-- Chat Revive: make the ping throttle configurable per guild.
--
-- The role-ping used to be a hard-coded "at most once per channel per 24h"
-- constant in logic.should_ping. Servers want their own cadence, so the two
-- dials now live on revive_guild_config:
--   * ping_max_per_day     -- how many times the role may be tagged per
--                             channel per local day (was effectively 1)
--   * ping_cooldown_minutes -- minimum spacing between two pings in a channel
--                             (was 1440; 0 means "no spacing, cap only")
-- Defaults reproduce the newly requested behaviour (3/day, 1h apart), not the
-- old once-a-day rule.

ALTER TABLE revive_guild_config
    ADD COLUMN ping_max_per_day INTEGER NOT NULL DEFAULT 3;
ALTER TABLE revive_guild_config
    ADD COLUMN ping_cooldown_minutes INTEGER NOT NULL DEFAULT 60;
