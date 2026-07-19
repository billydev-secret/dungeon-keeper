-- Paid quest rerolls (plan: economy-sinks-round-2 stage 1).
--
-- econ_rerolls started life as a pure allowance ledger: the row's existence
-- WAS the "free reroll spent today" flag, so it needed no counter. Paid
-- rerolls stack on top of the free one, so the day's row now also has to
-- count them for the per-day cap.
--
-- paid_count is rerolls bought AFTER the free one on that guild-local day;
-- a row with paid_count = 0 is the pre-existing "free reroll used" state,
-- which is exactly what backfilling the default gives us.

ALTER TABLE econ_rerolls ADD COLUMN paid_count INTEGER NOT NULL DEFAULT 0;
