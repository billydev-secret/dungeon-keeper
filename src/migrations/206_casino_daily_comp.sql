-- Migration 206: the casino's daily comp claim
-- (2026-09-04, docs/reviews/2026-09-02-games-deep-review-findings.md casino-134).
--
-- One house-funded slots spin a day from the hub, behind the casino_daily_comp
-- dial (default 0 = off). The claim has to be recorded so a second press on
-- the same guild-local day hands out nothing, and it has to be atomic — two
-- simultaneous presses must not both spin. casino_daily is already keyed by
-- (guild, member, guild-local day) and already carries the day roll the cap
-- uses, so the claim is one flag on that row rather than a new table: the
-- upsert flips 0 → 1 and a rowcount of 0 means "already had it". The row's
-- wagered stays 0 — the comp is not a wager and never touches the cap.
--
-- Per-user data: none new. casino_daily is registered under casino_* in
-- docs/data_register.md and purged with the rest of the casino tables.
--
-- Idempotent: the migration runner tolerates "duplicate column name".

ALTER TABLE casino_daily ADD COLUMN comp_claimed INTEGER NOT NULL DEFAULT 0;
